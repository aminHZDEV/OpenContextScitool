"""`okf init` -- scaffold a project so the bundle, the skill, and the MCP
server all exist and find each other.

Writes no knowledge. This is plumbing only: directories, config, and the
skill/subagent that let the agent you already have do the ingestion pass.
Nothing here calls an LLM.
"""

from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

CONTEXT_SRC = Path(__file__).parent / "ingest-context.md"

# `context: fork` + `agent:` runs the pass in a forked subagent, so the main
# session never sees the source documents -- ingestion reads everything and
# needs none of it afterwards.
SKILL_MD = """\
---
description: >-
  Ingest this project's documentation into the OKF bundle at ./bundle so it can
  be retrieved by search. Use when the bundle does not exist yet, or when docs
  have changed and the bundle is stale.
context: fork
agent: okf-ingest
allowed-tools: Read, Glob, Grep, Write, Edit
---

Follow the instruction document at `${CLAUDE_SKILL_DIR}/ingest-context.md`
EXACTLY. Read it in full before writing anything.

Source documents: @@DOCS@@
Output bundle:    ${CLAUDE_PROJECT_DIR}/bundle

Read every source document completely, then write OKF concept files into the
bundle per the instruction document -- including index.md and log.md.

Skip vendored trees entirely: @@SKIPS@@. Those contain other people's
markdown (licences, authors files) and are not this project's knowledge.

The instruction document is authoritative -- do not improvise beyond it. Where
sources are ambiguous or disagree, follow its honesty rules (section 8) rather
than picking whichever reads better.

When done, print one line: how many concepts, and their types. Then tell the
user to run `okf index` to make them searchable.
"""

AGENT_MD = """\
---
name: okf-ingest
description: >-
  Converts project documentation into an OKF concept bundle, following the
  project's ingest-context.md. Use for bulk documentation ingestion, or when
  the OKF bundle needs rebuilding from source docs.
tools: Read, Glob, Grep, Write, Edit
model: inherit
color: cyan
---

You convert source documents into an OKF bundle: a directory of markdown
concept files with YAML frontmatter, written to be retrieved by a keyword
(BM25) search index rather than browsed by a human.

You will be given a path to an instruction document. It is authoritative --
read it in full first and follow it exactly. Do not improvise beyond it.

Two things matter more than anything else:

1. Search is lexical. There are no embeddings. A concept is findable only if
   the searcher's words are literally in its frontmatter. Write `description`,
   `tags`, and `aliases` as the index they are, not as prose decoration.

2. Never invent. If a source doesn't say why, don't supply a plausible why.
   Mark inferred concepts `confidence: low`. Where two sources disagree, record
   both -- a confident wrong entry is worse than a missing one, because the
   reader cannot tell it apart from a right one.
"""


# The curator is the maintenance half of okf-ingest: ingest CREATES the bundle,
# the curator IMPROVES it using the usage telemetry `okf report` exposes. It
# needs Bash (to run `okf report` and `okf index`), which ingest does not.
CURATOR_SKILL_MD = """\
---
description: >-
  Improve an existing OKF bundle using its usage data. Reads `okf report` to
  find bad context -- questions with no answer, concepts offered but never read,
  concepts read then re-searched -- and fixes the concept markdown. Use after
  real traffic has accumulated, or when retrieval quality feels off.
context: fork
agent: okf-curator
allowed-tools: Bash, Read, Glob, Grep, Write, Edit
---

Improve the OKF bundle at `${CLAUDE_PROJECT_DIR}/bundle` using its usage data.

1. Run `okf report` and read every section. Each finding is a real defect.
2. Fix each by EDITING the concept markdown -- follow the authoring rules in
   `${CLAUDE_SKILL_DIR}/ingest-context.md`, especially the honesty rules:
   - gap (asked, nothing found): write the missing concept ONLY if a source
     supports it; if the docs genuinely don't answer it, note it for a human.
   - noise (offered, never read): the description oversells. Rewrite it to
     promise only what the concept delivers.
   - weak (low top score): a vocabulary miss -- add the searcher's exact words
     to `aliases`.
   - insufficient (read, then searched again): the concept failed to answer.
     Fix the content, or split it.
   - unmarked conflict: two concepts make rival claims with no `conflicts_with`.
     Add the reciprocal link on both, and a `Caveat` naming the conflict.
3. Run `okf index` so the edits become searchable.
4. Print a short summary: what you fixed, what you left for a human, and why.

Never invent knowledge to close a gap. A gap the docs don't cover is a note for
a human, not a concept for you to fabricate.
"""

CURATOR_AGENT_MD = """\
---
name: okf-curator
description: >-
  Maintains an existing OKF bundle by acting on its usage telemetry. Reads
  `okf report`, then edits concept markdown to fix oversold descriptions,
  missing aliases, incomplete concepts, and unmarked contradictions. Use to
  improve, audit, or clean up a bundle after it has seen real traffic.
tools: Bash, Read, Glob, Grep, Write, Edit
model: inherit
color: green
---

You maintain an OKF bundle -- a directory of markdown concept files retrieved by
a keyword (BM25) search index. You do not create the bundle (that is okf-ingest);
you improve one that exists, driven by what its usage log reveals.

Your evidence is `okf report`. From real queries and reads it surfaces gaps
(questions that returned nothing), noise (concepts search offers that nobody
opens), weak answers (queries whose best hit barely scored), and insufficient
concepts (opened, then followed by another search). Each is a defect with a
specific fix, and the report names which.

Two rules govern every edit:

1. Fix the markdown, never the database. The index is derived; `okf index`
   rebuilds it from the files, erasing any direct DB edit.

2. Never invent to close a gap. If the source material does not answer a
   question, that gap is a note for a human -- a fabricated concept is the one
   thing worse than a missing one, because the reader cannot tell.
"""


# MCP is an open protocol, so the server is portable. Only the config file
# differs per client -- and only Claude Code sets CLAUDE_PROJECT_DIR, so every
# other client needs explicit paths baked in at init time.
CLIENTS = {
    "claude": ".mcp.json",
    "cursor": ".cursor/mcp.json",
    "gemini": ".gemini/settings.json",
    "codex":  ".codex/config.toml",
}


def _merge_mcp(path: Path, args: list[str] | None) -> str:
    """Add our server to a JSON MCP config without clobbering what's there."""
    data = {}
    if path.exists():
        try:
            data = json.loads(path.read_text())
        except json.JSONDecodeError:
            return f"skipped ({path.name} is not valid JSON -- fix it first)"
    servers = data.setdefault("mcpServers", {})
    if "okf" in servers:
        return "already configured"
    entry: dict = {"command": "okf-serve"}
    if args:
        entry["args"] = args
    servers["okf"] = entry
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2) + "\n")
    return "added okf server"


def _codex_toml(path: Path, args: list[str]) -> str:
    """Codex uses TOML. Append rather than parse -- no stdlib TOML writer."""
    arglist = ", ".join(f'"{a}"' for a in args)
    block = f'\n[mcp_servers.okf]\ncommand = "okf-serve"\nargs = [{arglist}]\n'
    if path.exists() and "[mcp_servers.okf]" in path.read_text():
        return "already configured"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as f:
        f.write(block)
    return "appended okf server (verify the [mcp_servers] key for your codex version)"


def _write_skill(root: Path, name: str, body: str, docs: Path, force: bool) -> str:
    from .ingest import SKIP_DIRS

    d = root / ".claude" / "skills" / name
    d.mkdir(parents=True, exist_ok=True)
    f = d / "SKILL.md"
    if f.exists() and not force:
        return "exists (--force to overwrite)"
    # Not .format(): the body has literal ${CLAUDE_SKILL_DIR} that Claude Code
    # expands; .format() would try to read the braces as fields.
    f.write_text(body.replace("@@DOCS@@", str(docs))
                     .replace("@@SKIPS@@", ", ".join(sorted(SKIP_DIRS)[:6]) + ", ..."))
    # Copy the context doc in so the skill is self-contained and committable --
    # both skills reference it, so both dirs get a copy.
    shutil.copy(CONTEXT_SRC, d / "ingest-context.md")
    return "written"


def _write_agent(root: Path, name: str, body: str, force: bool) -> str:
    d = root / ".claude" / "agents"
    d.mkdir(parents=True, exist_ok=True)
    f = d / f"{name}.md"
    if f.exists() and not force:
        return "exists (--force to overwrite)"
    f.write_text(body)
    return "written"


def _append_gitignore(path: Path, entry: str) -> str:
    lines = path.read_text().splitlines() if path.exists() else []
    if entry in lines:
        return "already present"
    with path.open("a") as f:
        if lines and lines[-1].strip():
            f.write("\n")
        f.write(f"# okf: derived index, rebuild with `okf index`\n{entry}\n")
    return f"added {entry}"


def init(root: Path, docs: Path | None = None, force: bool = False,
         client: str = "claude") -> int:
    from .ingest import SKIP_DIRS, doc_files, find_docs

    root = root.resolve()
    if client not in CLIENTS and client != "all":
        print(f"Unknown client {client!r}. Choose from: {', '.join(CLIENTS)}, all",
              file=sys.stderr)
        return 1
    targets = list(CLIENTS) if client == "all" else [client]
    if docs is None:
        found = find_docs(root)
        docs = found[0] if found else root

    n = len(doc_files(docs))
    print(f"okf init: {root}")
    print(f"  docs:   {docs}  ({n} markdown files)")
    if n == 0:
        print("\n  No documentation found. This tool indexes prose docs;\n"
              "  for a code-only project, `/init` and CLAUDE.md are the better fit.",
              file=sys.stderr)
    elif n < 30:
        print(f"\n  note: {n} docs is small. Below ~30, grep usually beats this tool.\n"
              "  Scaffolding anyway, but benchmark before you rely on it.")

    (root / "bundle").mkdir(exist_ok=True)
    print("  bundle/ .................. ready")

    print("  .gitignore ............... " + _append_gitignore(root / ".gitignore", ".okf/"))

    # Only Claude Code sets CLAUDE_PROJECT_DIR; everyone else gets absolute paths.
    explicit = ["--bundle", str(root / "bundle"), "--db", str(root / ".okf" / "index.db")]
    for c in targets:
        cfg = root / CLIENTS[c]
        args = None if c == "claude" else explicit
        status = _codex_toml(cfg, explicit) if c == "codex" else _merge_mcp(cfg, args)
        print(f"  {CLIENTS[c]:<24} {status}")

    if "claude" not in targets:
        print("\n  (skills/agents are Claude Code-specific -- skipped; the MCP\n"
              "   tools and pasteable prompts below work in any client)")
        print("\nNext:")
        print(f"  1. okf ingest --docs {docs} --bundle {root / 'bundle'}")
        print("     ...or `okf prompt` and paste it into your agent.")
        print("  2. okf index")
        print("  Later, to maintain the bundle from usage data:")
        print("     okf prompt --curate    # paste into your agent")
        return 0

    # Two halves: okf-ingest creates the bundle, okf-curator maintains it.
    print("  .claude/skills/okf-ingest   " + _write_skill(root, "okf-ingest", SKILL_MD, docs, force) + "  (/okf-ingest)")
    print("  .claude/skills/okf-curator  " + _write_skill(root, "okf-curator", CURATOR_SKILL_MD, docs, force) + "  (/okf-curator)")
    print("  .claude/agents/okf-ingest   " + _write_agent(root, "okf-ingest", AGENT_MD, force))
    print("  .claude/agents/okf-curator  " + _write_agent(root, "okf-curator", CURATOR_AGENT_MD, force))

    print("\nNext:")
    print("  1. In Claude Code, run:  /okf-ingest      (or: okf ingest)")
    print("  2. Then:                 okf index")
    print("  3. Restart Claude Code so it picks up .mcp.json and the new skill.")
    print("\nCommit bundle/, .mcp.json and .claude/ -- they're shared. .okf/ is derived.")
    return 0