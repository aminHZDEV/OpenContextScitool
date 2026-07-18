"""Build the search index from an OKF bundle.

The bundle is the source of truth; this database is derived and disposable.
Rebuild it any time with `index --rebuild`.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import re
import sqlite3
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import yaml

RESERVED = {"index.md", "log.md"}
REQUIRED = ("type", "title", "description", "tags", "timestamp", "source", "confidence")
TYPES = {"Concept", "Metric", "Process", "Reference", "Decision", "System", "Caveat"}

# bm25 weights, positional per schema.sql: title, aliases, tags, description, body
WEIGHTS = (10.0, 8.0, 5.0, 3.0, 1.0)

LINK_RE = re.compile(r"\[[^\]]*\]\(([^)]+\.md)(?:#[^)]*)?\)")
AUTO_MARKER = "<!-- concepts:auto -->"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass
class Concept:
    path: str
    meta: dict
    body: str
    content_hash: str
    links: list[str] = field(default_factory=list)


class _StrictLoader(yaml.SafeLoader):
    """SafeLoader that rejects duplicate keys instead of silently dropping one.

    PyYAML's default keeps the LAST of a duplicate pair with no error. Someone
    editing frontmatter adds a second `aliases:`, everything reports success,
    and their edit is discarded — the worst kind of failure.
    """


def _no_dupes(loader, node, deep=False):
    seen = set()
    for k, _ in node.value:
        key = loader.construct_object(k, deep=deep)
        if key in seen:
            raise ValueError(f"duplicate key {key!r} in frontmatter "
                             f"(YAML would silently discard one of them)")
        seen.add(key)
    return yaml.SafeLoader.construct_mapping(loader, node, deep)


_StrictLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _no_dupes)


def split_frontmatter(text: str) -> tuple[dict, str]:
    """Return (frontmatter, body). Raises ValueError on anything unparseable.

    A bare `---` opener with no closer, or YAML that doesn't yield a mapping,
    is a hard error: the concept would index with no type and be unroutable.
    """
    if not text.startswith("---"):
        raise ValueError("no frontmatter")
    parts = text.split("---", 2)
    if len(parts) < 3:
        raise ValueError("unterminated frontmatter")
    meta = yaml.load(parts[1], Loader=_StrictLoader)
    if meta is None:
        raise ValueError("empty frontmatter")
    if not isinstance(meta, dict):
        # Almost always an unquoted `key: value` inside a description.
        raise ValueError(f"frontmatter is {type(meta).__name__}, not a mapping")
    return meta, parts[2].lstrip("\n")


def _listify(v) -> list[str]:
    """Frontmatter fields that may be a scalar or a list. Tolerate both."""
    if v is None:
        return []
    if isinstance(v, str):
        return [s.strip() for s in v.split(",") if s.strip()]
    if isinstance(v, (list, tuple)):
        return [str(s).strip() for s in v if str(s).strip()]
    return [str(v)]


def resolve_link(src_path: str, href: str) -> str:
    """Resolve a markdown href to a bundle-relative path.

    Bundle-absolute (`/a/b.md`) and relative (`./b.md`) are both OKF-legal.
    """
    if href.startswith("/"):
        return href.lstrip("/")
    return os.path.normpath(os.path.join(os.path.dirname(src_path), href)).replace(os.sep, "/")


def read_concept(bundle: Path, path: Path) -> Concept:
    raw = path.read_text(encoding="utf-8")
    rel = path.relative_to(bundle).as_posix()
    meta, body = split_frontmatter(raw)
    links = [resolve_link(rel, h) for h in LINK_RE.findall(body)]
    return Concept(
        path=rel,
        meta=meta,
        body=body,
        content_hash=hashlib.sha256(raw.encode("utf-8")).hexdigest(),
        links=links,
    )


def walk(bundle: Path):
    for p in sorted(bundle.rglob("*.md")):
        if p.name in RESERVED:
            continue
        yield p


# ------------------------------------------------------------------ validate


def validate(bundle: Path) -> tuple[list[str], list[str]]:
    """Execute the ingest-context.md §12 checklist. Returns (errors, warnings).

    Errors mean the bundle will not index correctly. Warnings mean it will
    index but retrieve badly.
    """
    errors: list[str] = []
    warns: list[str] = []
    concepts: list[Concept] = []
    seen: set[str] = set()

    for p in walk(bundle):
        rel = p.relative_to(bundle).as_posix()
        try:
            c = read_concept(bundle, p)
        except ValueError as e:
            errors.append(f"{rel}: {e}")
            continue
        concepts.append(c)
        seen.add(rel)

        for f in REQUIRED:
            if not c.meta.get(f):
                errors.append(f"{rel}: missing required field '{f}'")

        t = c.meta.get("type")
        if t and t not in TYPES:
            errors.append(f"{rel}: type '{t}' not in vocabulary {sorted(TYPES)}")

        desc = str(c.meta.get("description", ""))
        if len(desc.split()) > 30:
            warns.append(f"{rel}: description is {len(desc.split())} words (limit 30)")
        low = desc.lower()
        for filler in ("this document", "a guide to", "an overview of", "information about"):
            if low.startswith(filler):
                warns.append(f"{rel}: description opens with filler {filler!r}")

        n_tags = len(_listify(c.meta.get("tags")))
        if n_tags and not 4 <= n_tags <= 8:
            warns.append(f"{rel}: {n_tags} tags (want 4-8)")

    if not concepts:
        return errors, warns

    # Broken links are OKF-legal (§9) and used as to-do markers -- warn only.
    for c in concepts:
        for dst in c.links:
            if dst not in seen:
                warns.append(f"{c.path}: link to missing concept '{dst}'")
        for dst in _listify(c.meta.get("conflicts_with")):
            d = resolve_link(c.path, dst)
            if d not in seen:
                errors.append(f"{c.path}: conflicts_with points at missing '{d}'")
            else:
                back = [resolve_link(d, x) for x in _listify(
                    next(k.meta.get("conflicts_with") for k in concepts if k.path == d)
                )]
                if c.path not in back:
                    errors.append(f"{c.path}: conflicts_with '{d}' is not reciprocated")

    # §4: Reference is the default sink. If it dominates, type has stopped routing.
    n_ref = sum(1 for c in concepts if c.meta.get("type") == "Reference")
    pct = n_ref / len(concepts)
    if pct > 0.70:
        warns.append(
            f"Reference is {pct:.0%} of {len(concepts)} concepts (>70%) -- "
            "type is not routing; you likely swallowed Caveats into schema dumps"
        )

    return errors, warns


# ------------------------------------------------------------------- index


# Columns added after the first release. CREATE TABLE IF NOT EXISTS won't add
# them to an existing db, so every upgrade would break without this.
_MIGRATIONS = [
    ("query", "response_chars", "INTEGER NOT NULL DEFAULT 0"),
    ("read", "response_chars", "INTEGER NOT NULL DEFAULT 0"),
    ("read", "truncated", "INTEGER NOT NULL DEFAULT 0"),
]


def connect(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    schema = (Path(__file__).parent / "schema.sql").read_text()
    conn.executescript(schema)

    for table, col, decl in _MIGRATIONS:
        cols = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
        if col not in cols:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {decl}")
    conn.commit()
    return conn


def index(bundle: Path, db_path: Path, rebuild: bool = False) -> dict:
    conn = connect(db_path)
    stats = {"indexed": 0, "skipped": 0, "removed": 0}

    # Stamp the bundle this index belongs to, so the server can refuse to serve
    # it for a different project (see server.resolve()).
    conn.execute(
        "INSERT OR REPLACE INTO meta (key, value) VALUES ('bundle_path', ?)",
        (str(bundle.resolve()),),
    )

    if rebuild:
        conn.executescript("DELETE FROM concept; DELETE FROM edge; DELETE FROM concept_fts;")

    known = {r["path"]: r["content_hash"] for r in conn.execute("SELECT path, content_hash FROM concept")}
    on_disk: set[str] = set()

    for p in walk(bundle):
        rel = p.relative_to(bundle).as_posix()
        on_disk.add(rel)
        try:
            c = read_concept(bundle, p)
        except ValueError as e:
            print(f"  skip {rel}: {e}", file=sys.stderr)
            continue

        # The whole point of content_hash: unchanged files cost nothing.
        if known.get(rel) == c.content_hash:
            stats["skipped"] += 1
            continue

        tags = "\n".join(_listify(c.meta.get("tags")))
        aliases = "\n".join(_listify(c.meta.get("aliases")))
        source = "\n".join(_listify(c.meta.get("source")))

        conn.execute("DELETE FROM concept WHERE path = ?", (rel,))
        conn.execute("DELETE FROM concept_fts WHERE path = ?", (rel,))
        conn.execute("DELETE FROM edge WHERE src = ?", (rel,))

        conn.execute(
            """INSERT INTO concept (path, type, title, description, tags, aliases,
               source, confidence, timestamp, body, word_count, content_hash, indexed_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (rel, str(c.meta.get("type", "")), str(c.meta.get("title", "")),
             str(c.meta.get("description", "")), tags, aliases, source,
             str(c.meta.get("confidence", "")), str(c.meta.get("timestamp", "")),
             c.body, len(c.body.split()), c.content_hash, _now()),
        )
        conn.execute(
            "INSERT INTO concept_fts (title, aliases, tags, description, body, path) VALUES (?,?,?,?,?,?)",
            (str(c.meta.get("title", "")), aliases, tags, str(c.meta.get("description", "")), c.body, rel),
        )
        for dst in c.links:
            conn.execute("INSERT OR IGNORE INTO edge (src, dst, kind) VALUES (?,?,'link')", (rel, dst))
        for dst in _listify(c.meta.get("conflicts_with")):
            conn.execute("INSERT OR IGNORE INTO edge (src, dst, kind) VALUES (?,?,'conflicts_with')",
                         (rel, resolve_link(rel, dst)))
        stats["indexed"] += 1

    for gone in set(known) - on_disk:
        conn.execute("DELETE FROM concept WHERE path = ?", (gone,))
        conn.execute("DELETE FROM concept_fts WHERE path = ?", (gone,))
        conn.execute("DELETE FROM edge WHERE src = ?", (gone,))
        stats["removed"] += 1

    conn.commit()
    conn.close()
    return stats


# Words of matching body text to return with each hit. The whole point: a
# snippet often IS the answer, so the caller never needs a second `read` turn.
# Tool latency is ~40ms; a model turn is ~15s -- turns are the only real cost.
SNIPPET_TOKENS = 20


def search(db_path: Path, q: str, limit: int = 10) -> list[dict]:
    conn = connect(db_path)
    # bm25 returns negative, lower = better. Negate so bigger = better for humans.
    # snippet(-1) auto-picks whichever column actually matched.
    rows = conn.execute(
        f"""SELECT f.path, c.type, c.title, c.description, c.confidence,
                   snippet(concept_fts, -1, '', '', '…', {SNIPPET_TOKENS}) AS snippet,
                   -bm25(concept_fts, {','.join(str(w) for w in WEIGHTS)}) AS score
            FROM concept_fts f JOIN concept c ON c.path = f.path
            WHERE concept_fts MATCH ? ORDER BY score DESC LIMIT ?""",
        (q, limit),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


# -------------------------------------------------------------- index.md gen


def render_index(bundle: Path, db_path: Path) -> int:
    """Fill `<!-- concepts:auto -->` in every index.md from frontmatter.

    Hand-copying descriptions into the index makes a second copy that drifts
    (ingest-context.md §11), so the generator owns that region.
    """
    conn = connect(db_path)
    written = 0
    order = ["Caveat", "Decision", "Process", "Concept", "System", "Metric", "Reference"]

    for idx in bundle.rglob("index.md"):
        text = idx.read_text(encoding="utf-8")
        if AUTO_MARKER not in text:
            continue
        prefix = idx.parent.relative_to(bundle).as_posix()
        prefix = "" if prefix == "." else prefix + "/"

        rows = conn.execute(
            "SELECT path, type, title, description FROM concept WHERE path LIKE ? ORDER BY title",
            (prefix + "%",),
        ).fetchall()

        lines: list[str] = []
        for t in order:
            group = [r for r in rows if r["type"] == t]
            if not group:
                continue
            lines.append(f"\n## {t}\n")
            for r in group:
                href = "./" + r["path"][len(prefix):]
                lines.append(f"* [{r['title']}]({href}) - {r['description']}")

        start = text.index(AUTO_MARKER)
        end = start + len(AUTO_MARKER)
        idx.write_text(text[:end] + "\n" + "\n".join(lines) + "\n", encoding="utf-8")
        written += 1

    conn.close()
    return written


# --------------------------------------------------------------------- cli


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="okf")
    ap.add_argument("command",
                    choices=["init", "ingest", "prompt", "index", "check", "search",
                             "render", "report", "dashboard"])
    ap.add_argument("args", nargs="*")
    ap.add_argument("--bundle", default="bundle", type=Path)
    ap.add_argument("--db", default=Path(".okf/index.db"), type=Path)
    ap.add_argument("--rebuild", action="store_true")
    ap.add_argument("--docs", type=Path, help="ingest: source dir (default: ./docs, or cwd if it has .md)")
    ap.add_argument("--model", help="ingest: model for the claude CLI")
    ap.add_argument("--dry-run", action="store_true", help="ingest: print the command, don't run it")
    ap.add_argument("--all", action="store_true",
                    help="ingest: re-ingest every source, not just changed ones")
    ap.add_argument("--force", action="store_true", help="init: overwrite existing skill/agent files")
    ap.add_argument("--curate", action="store_true",
                    help="prompt: emit the curation prompt (maintain a bundle) instead of the ingest prompt")
    ap.add_argument("--json", action="store_true", help="report: machine-readable output")
    ap.add_argument("--out", type=Path, help="dashboard: output html path")
    ap.add_argument("--no-open", action="store_true", help="dashboard: don't open a browser")
    ap.add_argument("--serve", action="store_true",
                    help="dashboard: run a local server so concepts are editable")
    ap.add_argument("--port", type=int, default=8420, help="dashboard --serve: port")
    ap.add_argument("--client", default="claude",
                    choices=["claude", "cursor", "gemini", "codex", "all"],
                    help="init: which MCP client to configure (default: claude)")
    # Flags interleave with the variadic query: `search --db X foo bar`.
    # parse_args() can't do that; parse_intermixed_args() can.
    a = ap.parse_intermixed_args(argv)

    if a.command == "init":
        from .init import init as run_init

        return run_init(Path.cwd(), docs=a.docs, force=a.force, client=a.client)

    if a.command == "dashboard":
        if a.serve:
            from .serve_dash import serve

            return serve(a.bundle, a.db, port=a.port)
        from .dashboard import dashboard as run_dash

        return run_dash(a.db, a.bundle, out=a.out, open_it=not a.no_open)

    if a.command == "report":
        from .report import report as run_report

        return run_report(a.db, as_json=a.json)

    if a.command == "prompt":
        if a.curate:
            from .ingest import build_curate_prompt

            print(build_curate_prompt(a.bundle, a.db))
            return 0
        from .ingest import build_prompt, find_docs

        docs = a.docs or (find_docs(Path.cwd()) or [Path.cwd()])[0]
        print(build_prompt(docs, a.bundle))
        return 0

    if a.command == "ingest":
        from .ingest import find_docs, ingest as run_ingest

        docs = a.docs
        if docs is None:
            found = find_docs(Path.cwd())
            if not found:
                print("No ./docs directory and no .md files here. Pass --docs.", file=sys.stderr)
                return 1
            docs = found[0]
            print(f"using --docs {docs}", file=sys.stderr)

        rc = run_ingest(docs, a.bundle, db=a.db, model=a.model,
                        dry_run=a.dry_run, force_all=a.all)
        if rc or a.dry_run:
            return rc
        # Ingestion is only half the job -- an unindexed bundle serves nothing.
        print("\n--- indexing ---", file=sys.stderr)
        return main(["index", "--bundle", str(a.bundle), "--db", str(a.db)])

    if a.command == "check":
        errors, warns = validate(a.bundle)
        for w in warns:
            print(f"warn: {w}")
        for e in errors:
            print(f"ERROR: {e}", file=sys.stderr)
        print(f"\n{len(errors)} errors, {len(warns)} warnings")
        return 1 if errors else 0

    if a.command == "index":
        errors, warns = validate(a.bundle)
        if errors:
            for e in errors:
                print(f"ERROR: {e}", file=sys.stderr)
            print(f"\n{len(errors)} errors -- fix them or run `check` to see all", file=sys.stderr)
            return 1
        for w in warns:
            print(f"warn: {w}")
        s = index(a.bundle, a.db, a.rebuild)
        print(f"indexed {s['indexed']}, skipped {s['skipped']} unchanged, removed {s['removed']}")
        return 0

    if a.command == "render":
        print(f"rendered {render_index(a.bundle, a.db)} index.md file(s)")
        return 0

    if a.command == "search":
        if not a.args:
            print("usage: search <query>", file=sys.stderr)
            return 2
        hits = search(a.db, " ".join(a.args))
        if not hits:
            print("no results")
            return 0
        # Snippets only on the top few -- the answer is almost always there, and
        # a snippet on every hit bloats the payload (see server.SNIPPET_HITS).
        for i, h in enumerate(hits, 1):
            print(f"{i:2}. [{h['score']:6.2f}] {h['type']:<10} {h['title']}")
            print(f"     {h['path']}")
            print(f"     {h['description']}")
            if i <= 3:
                snip = " ".join((h.get("snippet") or "").split())
                if snip:
                    print(f"     …{snip}…")
        return 0

    return 2


if __name__ == "__main__":
    sys.exit(main())