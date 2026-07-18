"""MCP server over an OKF bundle.

Every tool call is logged. That is half the point: retrieval through tools is
an observable boundary, so we can measure which docs get offered, which get
taken, and which questions have no answer. Content pasted into a prompt is
unmeasurable; content fetched through here is not.

Run:  okf-serve --bundle ./bundle --db ./.okf/index.db
"""

from __future__ import annotations

import argparse
import os
import sqlite3
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

try:
    from mcp.server.fastmcp import FastMCP
except ModuleNotFoundError:  # installed without the [server] extra
    raise SystemExit(
        "The MCP server needs the 'server' extra:\n"
        "    pipx install 'okf-ctx[server]'      # or\n"
        "    pip install 'okf-ctx[server]'\n"
        "Indexing and search (`okf`) work without it."
    )

from .indexer import SNIPPET_TOKENS, WEIGHTS, connect

# Snippets only on the top hits. Beyond this, the payload cost outweighs the
# shrinking chance the answer is that far down. Measured, not guessed.
SNIPPET_HITS = 3

def project_root() -> Path:
    """Find the project this server should serve.

    Claude Code sets CLAUDE_PROJECT_DIR in the spawned server's environment and
    documents that you should NOT depend on the working directory -- so that
    variable wins. Falling back on cwd would make one system-wide install
    answer every project with whichever bundle it happened to start in.
    """
    if p := os.environ.get("CLAUDE_PROJECT_DIR"):
        return Path(p).resolve()
    # Not under Claude Code (direct CLI run): walk up for a marker, like git.
    cur = Path.cwd().resolve()
    for d in (cur, *cur.parents):
        if (d / ".okf").is_dir() or (d / "bundle").is_dir():
            return d
    return cur


ROOT = project_root()
BUNDLE = Path(os.environ.get("OKF_BUNDLE") or ROOT / "bundle")
DB = Path(os.environ.get("OKF_DB") or ROOT / ".okf" / "index.db")

# One session per server process. A client reconnecting starts a new session,
# which is what we want: session is the unit we measure "did they search again"
# against, and a reconnect is a new train of thought.
SESSION_ID = uuid.uuid4().hex[:12]

mcp = FastMCP("okf-context")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _db() -> sqlite3.Connection:
    conn = connect(DB)
    conn.execute(
        "INSERT OR IGNORE INTO session (id, started_at, client) VALUES (?,?,?)",
        (SESSION_ID, _now(), "mcp"),
    )
    return conn


def _safe_path(path: str) -> Path:
    """Resolve a caller-supplied path, confined to the bundle.

    `path` arrives from a language model. Without this, `../../.ssh/id_rsa`
    reads whatever the process can reach.
    """
    root = BUNDLE.resolve()
    target = (root / path.lstrip("/")).resolve()
    if not target.is_relative_to(root):
        raise ValueError(f"path escapes bundle: {path}")
    return target


@mcp.tool()
def search(query: str, limit: int = 8, type: str | None = None) -> str:
    """Search the knowledge bundle for concepts matching a query.

    Search is keyword-based (BM25), not semantic: it matches the words you
    type against concept titles, aliases, tags, and descriptions. If the first
    query misses, retry with different vocabulary rather than rephrasing the
    sentence.

    Args:
        query: Words to search for. Prefer distinctive nouns over prose.
        limit: Max results (default 8).
        type: Optionally filter to one of Concept, Metric, Process, Reference,
            Decision, System, Caveat. Use "Caveat" to list known traps.
    """
    conn = _db()
    w = ",".join(str(x) for x in WEIGHTS)
    sql = f"""SELECT f.path, c.type, c.title, c.description, c.confidence,
                     snippet(concept_fts, -1, '', '', '…', {SNIPPET_TOKENS}) AS snip,
                     -bm25(concept_fts, {w}) AS score
              FROM concept_fts f JOIN concept c ON c.path = f.path
              WHERE concept_fts MATCH ?"""
    params: list = [query]
    if type:
        sql += " AND c.type = ?"
        params.append(type)
    sql += " ORDER BY score DESC LIMIT ?"
    params.append(limit)

    try:
        rows = conn.execute(sql, params).fetchall()
    except sqlite3.OperationalError as e:
        # FTS5 syntax errors (bare `AND`, unbalanced quotes) are the caller's
        # problem to fix, so tell them rather than dying.
        conn.close()
        return f"Invalid search syntax: {e}. Try plain words without operators."

    cur = conn.execute(
        "INSERT INTO query (session_id, ts, text, n_results, top_score) VALUES (?,?,?,?,?)",
        (SESSION_ID, _now(), query, len(rows), rows[0]["score"] if rows else None),
    )
    qid = cur.lastrowid
    for i, r in enumerate(rows, 1):
        conn.execute(
            "INSERT OR IGNORE INTO retrieval (query_id, concept_path, rank, score) VALUES (?,?,?,?)",
            (qid, r["path"], i, r["score"]),
        )

    def _finish(text: str) -> str:
        # Measure the payload we're about to hand back -- that string IS the
        # context this call consumed. Recorded after building it, necessarily.
        conn.execute("UPDATE query SET response_chars = ? WHERE id = ?", (len(text), qid))
        conn.commit()
        conn.close()
        return text

    if not rows:
        # Terse on purpose: this text is re-paid on every miss.
        return _finish(f"No match for {query!r}. Keyword index — retry with "
                       f"different words for the same idea, or one distinctive noun.")

    # Snippets are the round-trip killer -- often the answer is right there, so
    # the caller never needs a read() turn. But a snippet on every one of 8 hits
    # made search results 76% of the whole context budget (measured), and the
    # answer is almost always in the top hit. So: snippet the top few, list the
    # rest as title+path the caller can read() if the top ones miss.
    out = []
    for i, r in enumerate(rows, 1):
        flag = " [confidence:low]" if r["confidence"] == "low" else ""
        line = (f"{i}. {r['title']} ({r['type']}){flag}\n"
                f"   {r['path']}\n"
                f"   {r['description']}")
        if i <= SNIPPET_HITS:
            snip = " ".join((r["snip"] or "").split())
            if snip:
                line += f"\n   …{snip}…"
        out.append(line)
    out.append("\nTop snippets may already answer it. read(path) for the full concept.")
    return _finish("\n".join(out))


@mcp.tool()
def read(path: str, max_words: int = 1200) -> str:
    """Read the full text of one concept.

    Args:
        path: The concept path from a search result, e.g. "auth/rotate-key.md".
        max_words: Truncate the body beyond this. Raise it if you got a
            truncation marker and still need the rest.
    """
    conn = _db()
    row = conn.execute(
        "SELECT path, type, title, description, source, confidence, body FROM concept WHERE path = ?",
        (path.lstrip("/"),),
    ).fetchone()

    if row is None:
        conn.close()
        return f"No concept at {path!r}. Use search() to find the right path."

    # Attribute the read to the most recent query in this session, so the
    # retrieval->read join can tell "offered" from "taken".
    q = conn.execute(
        "SELECT id FROM query WHERE session_id = ? ORDER BY id DESC LIMIT 1", (SESSION_ID,)
    ).fetchone()
    cur = conn.execute(
        "INSERT INTO read (session_id, ts, concept_path, query_id) VALUES (?,?,?,?)",
        (SESSION_ID, _now(), row["path"], q["id"] if q else None),
    )
    rid = cur.lastrowid

    conflicts = [r["dst"] for r in conn.execute(
        "SELECT dst FROM edge WHERE src = ? AND kind = 'conflicts_with'", (row["path"],)
    )]

    head = [f"# {row['title']}", f"type: {row['type']}  |  confidence: {row['confidence']}"]
    if row["source"]:
        head.append("source: " + ", ".join(row["source"].split("\n")))
    if conflicts:
        # Load-bearing: the reader has no other way to learn a rival answer
        # exists. A lexical index cannot express "and there is a competing claim".
        head.append("\n⚠ CONFLICTS WITH (a rival answer to the same question): "
                    + ", ".join(conflicts))

    # A concept is meant to fit on a screen, but nothing enforces that -- and an
    # oversized one would silently spend the caller's context. Bound it visibly.
    words = row["body"].split()
    body = row["body"]
    truncated = len(words) > max_words
    if truncated:
        body = (" ".join(words[:max_words])
                + f"\n\n[truncated at {max_words} of {len(words)} words — "
                  f"call read({path!r}, max_words={len(words)}) for the rest]")

    out = "\n".join(head) + "\n\n" + body
    conn.execute("UPDATE read SET response_chars = ?, truncated = ? WHERE id = ?",
                 (len(out), int(truncated), rid))
    conn.commit()
    conn.close()
    return out


@mcp.tool()
def links(path: str, direction: str = "out") -> str:
    """List concepts linked from (or to) a concept.

    Args:
        path: The concept path.
        direction: "out" for what it links to, "in" for what links to it.
    """
    conn = _db()
    p = path.lstrip("/")
    if direction == "in":
        rows = conn.execute(
            """SELECT e.src AS other, e.kind, c.title FROM edge e
               LEFT JOIN concept c ON c.path = e.src WHERE e.dst = ?""", (p,)).fetchall()
    else:
        rows = conn.execute(
            """SELECT e.dst AS other, e.kind, c.title FROM edge e
               LEFT JOIN concept c ON c.path = e.dst WHERE e.src = ?""", (p,)).fetchall()
    conn.close()

    if not rows:
        return f"No {direction}bound links for {path!r}."
    out = []
    for r in rows:
        # title is NULL when the target doesn't exist -- OKF tolerates broken
        # links as to-do markers, so surface rather than hide them.
        title = r["title"] or "(not written yet)"
        kind = "" if r["kind"] == "link" else f"  [{r['kind']}]"
        out.append(f"- {title}{kind}\n  path: {r['other']}")
    return "\n".join(out)


@mcp.tool()
def report() -> str:
    """Diagnose the knowledge bundle: what's missing, misleading, or dead.

    Reads the usage log to find bad context — questions with no answer,
    concepts search keeps offering that nobody opens, concepts that got opened
    and failed to answer. Use this when asked to improve, audit, or fix the
    docs, then edit the concept files in the bundle to address what it reports.

    The findings are evidence, not orders: a "gap" may be knowledge that
    genuinely belongs elsewhere, and "dead weight" may just be new.
    """
    from .report import collect, render

    r = collect(DB)
    body = render(r)
    if r["totals"]["queries"] == 0:
        return body
    return body + (
        f"\n\nBundle is at {BUNDLE} — the concept files are plain markdown; "
        "edit them directly.\n"
        "Fixes, in rough order of value:\n"
        "  gaps      -> write the missing concept (or accept it's out of scope)\n"
        "  weak      -> add `aliases` with the words the searcher actually used\n"
        "  noise     -> rewrite the description to promise only what it delivers\n"
        "  insufficient -> the concept is wrong or incomplete; fix the content\n"
        "Re-run `okf index` after editing, or the changes won't be searchable."
    )


def main(argv=None) -> int:
    global BUNDLE, DB
    ap = argparse.ArgumentParser(prog="okf-serve")
    ap.add_argument("--bundle", type=Path, default=BUNDLE)
    ap.add_argument("--db", type=Path, default=DB)
    a = ap.parse_args(argv)

    BUNDLE, DB = a.bundle.resolve(), a.db

    if not BUNDLE.is_dir():
        print(f"No bundle at {BUNDLE}\n"
              f"(project root resolved to {ROOT}"
              f"{' via CLAUDE_PROJECT_DIR' if os.environ.get('CLAUDE_PROJECT_DIR') else ' via cwd walk-up'})\n"
              f"Nothing to serve. Run `okf index` first, or pass --bundle.", file=sys.stderr)
        return 1
    if not DB.exists():
        print(f"No index at {DB}. Run: okf index --bundle {BUNDLE} --db {DB}", file=sys.stderr)
        return 1

    # Refuse a database built from a different bundle. Without this, a stale
    # --db or a copied .okf/ answers this project's questions with another
    # project's docs -- wrong, and silently so.
    conn = connect(DB)
    row = conn.execute("SELECT value FROM meta WHERE key = 'bundle_path'").fetchone()
    conn.close()
    if row and Path(row["value"]) != BUNDLE:
        print(f"Index/bundle mismatch — refusing to serve.\n"
              f"  index was built from: {row['value']}\n"
              f"  but bundle is:        {BUNDLE}\n"
              f"Run `okf index --bundle {BUNDLE} --db {DB}` to rebuild.", file=sys.stderr)
        return 1

    print(f"okf-serve: {BUNDLE}  ({DB})", file=sys.stderr)
    mcp.run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())