"""Drive the ingestion pass by shelling out to the `claude` CLI.

The library makes no API calls of its own. You already have an authenticated
agent on this machine -- `okf ingest` hands it the instruction document and
gets out of the way. No API key, no second client, no cost line.

The agent writes markdown to ./bundle; we index it afterwards.
"""

from __future__ import annotations

import hashlib
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

CONTEXT = Path(__file__).parent / "ingest-context.md"

# Vendored trees are full of .md that is not your knowledge: LICENSE.md and
# AUTHORS.md from site-packages, node_modules readmes, .git internals. Ingesting
# them mints concepts for other people's licences and burns tokens doing it.
SKIP_DIRS = {
    ".venv", "venv", ".env", "env", "node_modules", ".git", "__pycache__",
    "site-packages", "dist-info", "egg-info", ".tox", "build", "dist",
    "vendor", "third_party", ".mypy_cache", ".pytest_cache", "target",
}


def doc_files(root: Path) -> list[Path]:
    """Every .md under root that is plausibly *your* documentation."""
    out = []
    for p in root.rglob("*.md"):
        if any(part in SKIP_DIRS or part.endswith((".dist-info", ".egg-info"))
               for part in p.relative_to(root).parts):
            continue
        out.append(p)
    return sorted(out)

# The agent needs to read the sources and write the bundle. Nothing else --
# no Bash, no network. If the ingestion wants to run commands, that is a bug
# in the instruction document, not a permission to grant.
ALLOWED = ["Read", "Glob", "Grep", "Write", "Edit"]

PROMPT = """\
Follow the instruction document at {context} EXACTLY. Read it in full first.

Output bundle: {bundle}

Read these {n} source documents completely -- and ONLY these. Do not glob for
more; this list is curated and excludes vendored and third-party files.

{filelist}

Then write OKF concept files into the bundle directory per the instruction
document -- including index.md and log.md. Create the bundle if it doesn't exist.

The instruction document is authoritative. Do not improvise beyond it. If
something in the sources is ambiguous or two sources disagree, follow its
honesty rules (section 8) rather than picking whichever reads better.

When you are done, print a one-line summary: how many concepts, and their types.
"""


def find_docs(root: Path) -> list[Path]:
    """Guess where the docs are. Wrong often enough that --docs exists."""
    hits = []
    for cand in ("docs", "doc", "documentation"):
        d = root / cand
        if d.is_dir():
            hits.append(d)
    if not hits and list(root.glob("*.md")):
        hits.append(root)
    return hits


def _hash(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def triage(docs: Path, db: Path) -> dict:
    """Split the source docs by what actually needs re-reading.

    The whole point of incremental ingest: an edit to 1 doc out of 50 should
    cost 1 doc, not 50. Without this the ingestion pass is re-paid in full on
    every change, and the amortization argument for this tool collapses.
    """
    from .indexer import connect

    files = doc_files(docs)
    if not db.exists():
        return {"new": files, "changed": [], "unchanged": [], "deleted": [],
                "concepts_of": {}}

    conn = connect(db)
    known = {r["path"]: r["content_hash"]
             for r in conn.execute("SELECT path, content_hash FROM source_file")}

    new, changed, unchanged = [], [], []
    for f in files:
        k = str(f.resolve())
        if k not in known:
            new.append(f)
        elif known[k] != _hash(f):
            changed.append(f)
        else:
            unchanged.append(f)

    on_disk = {str(f.resolve()) for f in files}
    deleted = sorted(set(known) - on_disk)

    # Which existing concepts came from each stale source? `source` is a
    # newline-joined list of the paths the concept was derived from, so the
    # agent can be told to update those rather than mint duplicates.
    concepts_of: dict[str, list[str]] = {}
    for f in changed:
        # Match on the doc-dir-qualified relative path, not the bare basename:
        # a repo with both README.md and devops/README.md would otherwise pull
        # in every concept from both. "DEX-AI/README.md" does not appear inside
        # "DEX-AI/devops/README.md", so the qualified form disambiguates.
        try:
            needle = f"{docs.name}/{f.relative_to(docs)}"
        except ValueError:
            needle = f.name
        rows = conn.execute(
            "SELECT path FROM concept WHERE source LIKE ?", (f"%{needle}%",)).fetchall()
        if not rows and needle != f.name:
            # The ingesting agent chose some other path convention -- fall back
            # to the basename and accept over-inclusion over missing an update.
            rows = conn.execute(
                "SELECT path FROM concept WHERE source LIKE ?", (f"%{f.name}%",)).fetchall()
        if rows:
            concepts_of[str(f.resolve())] = [r["path"] for r in rows]
    conn.close()

    return {"new": new, "changed": changed, "unchanged": unchanged,
            "deleted": deleted, "concepts_of": concepts_of}


def record_sources(files: list[Path], db: Path) -> None:
    """Stamp the hashes -- only after the pass actually succeeded."""
    from .indexer import connect

    conn = connect(db)
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    conn.executemany(
        "INSERT OR REPLACE INTO source_file (path, content_hash, ingested_at) "
        "VALUES (?,?,?)",
        [(str(f.resolve()), _hash(f), now) for f in files])
    conn.commit()
    conn.close()


CURATE_PROMPT = """\
Improve the OKF bundle at {bundle} using its usage data.

1. Run this and read every section -- each finding is a real defect:
     okf report --db {db}

2. Fix each finding by EDITING the concept markdown under {bundle}. Follow OKF
   authoring rules; above all, never invent -- if the docs don't answer a
   question, note it for a human rather than fabricating a concept.
   - gap (asked, nothing found): write the missing concept only if a source
     supports it; otherwise leave a note.
   - noise (offered, never read): the description oversells -- rewrite it to
     promise only what the concept delivers.
   - weak (low top score): a vocabulary miss -- add the searcher's exact words
     to the concept's `aliases`.
   - insufficient (read, then searched again): the concept failed to answer --
     fix the content or split it.
   - unmarked conflict: two concepts make rival claims with no `conflicts_with`
     -- add the reciprocal link on both and a `Caveat` naming the conflict.

3. Edit the MARKDOWN, never the database -- the index is derived and `okf index`
   rebuilds it from the files, erasing any direct DB edit.

4. Re-index so the edits become searchable:
     okf index --bundle {bundle} --db {db}

5. Print a short summary: what you fixed, what you left for a human, and why.
"""


def build_curate_prompt(bundle: Path, db: Path) -> str:
    """The curation workflow as agent-agnostic paste text.

    okf-curator is a Claude Code skill/agent; this is the portable equivalent
    for Cursor, Codex, Gemini, or any agent that can run the CLI and edit files.
    """
    return CURATE_PROMPT.format(bundle=bundle.resolve(), db=db)


def build_prompt(docs: Path, bundle: Path) -> str:
    """The ingestion prompt, agent-agnostic.

    `okf ingest` feeds this to the `claude` CLI, but any agent that can read
    and write files can execute it -- Cursor, Codex, Gemini CLI. `okf prompt`
    just prints it.
    """
    files = doc_files(docs)
    return PROMPT.format(
        context=CONTEXT,
        bundle=bundle.resolve(),
        n=len(files),
        filelist="\n".join(f"- {f.resolve()}" for f in files),
    )


UPDATE_NOTE = """\

INCREMENTAL PASS — the bundle already exists. Only the sources listed above are
new or changed; every other concept in the bundle is current and must be left
alone. Do not re-read or rewrite them.

These existing concepts derive from the changed sources — UPDATE them in place
rather than creating duplicates:
{existing}
"""


def ingest(
    docs: Path,
    bundle: Path,
    db: Path | None = None,
    model: str | None = None,
    dry_run: bool = False,
    force_all: bool = False,
    extra: list[str] | None = None,
) -> int:
    if shutil.which("claude") is None:
        print(
            "The `claude` CLI is not on your PATH.\n"
            "okf ingest drives the agent you already have rather than calling an\n"
            "API itself. Install Claude Code, or run the ingestion by hand:\n"
            f'    claude "Follow {CONTEXT} and ingest {docs} into {bundle}"',
            file=sys.stderr,
        )
        return 1

    if not docs.exists():
        print(f"No such source directory: {docs}", file=sys.stderr)
        return 1

    all_files = doc_files(docs)
    if not all_files:
        print(f"No .md files under {docs} -- nothing to ingest.\n"
              f"(vendored dirs are skipped: {', '.join(sorted(SKIP_DIRS))})", file=sys.stderr)
        return 1
    if len(all_files) < 30:
        # Being honest about this up front is cheaper than a benchmark that
        # tells them the same thing after they've paid for an ingestion pass.
        print(f"note: only {len(all_files)} docs. Below ~30, grep usually beats "
              f"this tool.\n", file=sys.stderr)

    db = db or Path(".okf/index.db")
    t = {"new": all_files, "changed": [], "unchanged": [], "deleted": [],
         "concepts_of": {}} if force_all else triage(docs, db)

    files = t["new"] + t["changed"]
    if t["unchanged"]:
        print(f"incremental: {len(t['new'])} new, {len(t['changed'])} changed, "
              f"{len(t['unchanged'])} unchanged (skipped)", file=sys.stderr)
    if t["deleted"]:
        # Don't delete concepts automatically -- a source can move, and silently
        # dropping knowledge is worse than a stale note.
        print(f"warn: {len(t['deleted'])} source(s) gone; their concepts remain:",
              file=sys.stderr)
        for d in t["deleted"][:5]:
            print(f"        {d}", file=sys.stderr)
    if not files:
        print("nothing to ingest — all sources unchanged. (--all to force)")
        return 0

    bundle.mkdir(parents=True, exist_ok=True)
    prompt = PROMPT.format(
        context=CONTEXT,
        bundle=bundle.resolve(),
        n=len(files),
        filelist="\n".join(f"- {f.resolve()}" for f in files),
    )
    if t["unchanged"] and t["concepts_of"]:
        def _label(src: str) -> str:
            # Doc-relative, not basename: "devops/README.md" vs "README.md".
            try:
                return str(Path(src).relative_to(docs.resolve()))
            except ValueError:
                return Path(src).name

        existing = "\n".join(
            f"- from {_label(src)}: " + ", ".join(cs)
            for src, cs in t["concepts_of"].items())
        prompt += UPDATE_NOTE.format(existing=existing)
    elif t["unchanged"]:
        prompt += ("\nINCREMENTAL PASS — the bundle already exists and every "
                   "concept not derived from the sources above is current. "
                   "Leave them alone.\n")

    cmd = [
        "claude", "-p", prompt,
        "--permission-mode", "acceptEdits",   # it must write the bundle unattended
        "--allowedTools", *ALLOWED,
        "--add-dir", str(docs.resolve()),
        "--add-dir", str(bundle.resolve()),
    ]
    if model:
        cmd += ["--model", model]
    if extra:
        cmd += extra

    if dry_run:
        print("would run:\n")
        print("  " + " ".join(repr(c) if " " in c or "\n" in c else c for c in cmd))
        return 0

    print(f"ingesting {len(files)} doc(s) from {docs} -> {bundle}", file=sys.stderr)
    print("(this drives your local `claude`; it can take a while on a big doc set)\n",
          file=sys.stderr)
    try:
        rc = subprocess.run(cmd).returncode
    except KeyboardInterrupt:
        # Hashes NOT recorded: a half-written bundle must re-ingest those files.
        print("\ninterrupted -- the bundle may be half-written; re-run to continue",
              file=sys.stderr)
        return 130
    if rc == 0:
        record_sources(files, db)
    return rc