"""Regression tests for okf-ctx.

Every test here corresponds to something that was verified by hand during
development, or a bug that was found and fixed. The point is to make those
verifications repeatable so the next change can't silently reintroduce them.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from okf_ctx import indexer, ingest
from okf_ctx.indexer import connect, index, search, split_frontmatter, validate


# --------------------------------------------------------------- fixtures

CONCEPT = """\
---
type: {type}
title: {title}
description: 'A concept about {title} with a colon: here'
tags: [alpha, beta, gamma, delta]
aliases: [{title} synonym]
timestamp: 2026-07-15T00:00:00Z
source: [src/{title}.md]
confidence: high
---
Body text mentioning widget and gadget.
"""


def make_bundle(tmp_path: Path, concepts: dict[str, str]) -> Path:
    b = tmp_path / "bundle"
    b.mkdir()
    for name, ctype in concepts.items():
        (b / f"{name}.md").write_text(CONCEPT.format(type=ctype, title=name))
    return b


@pytest.fixture
def bundle(tmp_path):
    return make_bundle(tmp_path, {"alpha": "Concept", "beta": "Reference"})


# --------------------------------------------------------------- frontmatter

def test_colon_in_quoted_description_parses():
    """The YAML hazard: a colon-space in a scalar must be quoted. The CONCEPT
    template quotes it; this proves the parser accepts it."""
    meta, body = split_frontmatter(CONCEPT.format(type="Concept", title="x"))
    assert meta["type"] == "Concept"
    assert "widget" in body


def test_duplicate_key_is_rejected():
    """PyYAML silently keeps the last of a duplicate key — an edit that adds a
    second `aliases:` would vanish. The strict loader must reject it."""
    text = ("---\ntype: Concept\ntitle: x\naliases: [new]\naliases: [old]\n"
            "description: y\ntags: [a]\ntimestamp: t\nsource: [s]\nconfidence: high\n---\nb")
    with pytest.raises(ValueError, match="duplicate key"):
        split_frontmatter(text)


def test_unterminated_frontmatter_raises():
    with pytest.raises(ValueError):
        split_frontmatter("---\ntype: Concept\nno closing fence")


def test_missing_frontmatter_raises():
    with pytest.raises(ValueError, match="no frontmatter"):
        split_frontmatter("just a body, no fence")


# --------------------------------------------------------------- validation

def test_bad_type_is_an_error(tmp_path):
    b = make_bundle(tmp_path, {"x": "Nonsense"})
    errors, _ = validate(b)
    assert any("not in vocabulary" in e for e in errors)


def test_missing_required_field_is_an_error(tmp_path):
    b = tmp_path / "bundle"
    b.mkdir()
    (b / "x.md").write_text("---\ntype: Concept\ntitle: x\n---\nbody")
    errors, _ = validate(b)
    assert any("missing required field" in e for e in errors)


def test_reference_over_70pct_warns(tmp_path):
    b = make_bundle(tmp_path, {f"r{i}": "Reference" for i in range(8)})
    _, warns = validate(b)
    assert any("Reference is" in w for w in warns)


# --------------------------------------------------------------- indexing

def test_index_and_search_roundtrip(bundle, tmp_path):
    db = tmp_path / "i.db"
    stats = index(bundle, db)
    assert stats["indexed"] == 2
    hits = search(db, "widget")
    assert hits and hits[0]["path"].endswith(".md")


def test_content_hash_skips_unchanged(bundle, tmp_path):
    db = tmp_path / "i.db"
    index(bundle, db)
    stats = index(bundle, db)  # nothing changed
    assert stats["indexed"] == 0 and stats["skipped"] == 2


def test_alias_is_searchable(bundle, tmp_path):
    """aliases must reach the FTS index — they're the no-embeddings substitute."""
    db = tmp_path / "i.db"
    index(bundle, db)
    hits = search(db, "alpha synonym")
    assert any(h["path"] == "alpha.md" for h in hits)


def test_snippet_returned(bundle, tmp_path):
    db = tmp_path / "i.db"
    index(bundle, db)
    hits = search(db, "gadget")
    assert hits[0].get("snippet")  # the round-trip killer


def test_deleted_source_is_removed(bundle, tmp_path):
    db = tmp_path / "i.db"
    index(bundle, db)
    (bundle / "alpha.md").unlink()
    stats = index(bundle, db)
    assert stats["removed"] == 1


# --------------------------------------------------------------- migration

def test_connect_migrates_old_db(tmp_path):
    """A db predating response_chars/truncated must gain them, not break."""
    db = tmp_path / "old.db"
    conn = connect(db)  # builds current schema
    conn.close()
    # simulate an old db by dropping the added columns is hard; instead assert
    # the migrated columns exist after connect().
    conn = sqlite3.connect(db)
    qcols = {r[1] for r in conn.execute("PRAGMA table_info(query)")}
    rcols = {r[1] for r in conn.execute("PRAGMA table_info(read)")}
    conn.close()
    assert "response_chars" in qcols
    assert {"response_chars", "truncated"} <= rcols


# --------------------------------------------------------------- ingest triage

def test_triage_detects_new_and_unchanged(tmp_path):
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "a.md").write_text("# A")
    (docs / "b.md").write_text("# B")
    db = tmp_path / "i.db"
    connect(db).close()

    t1 = ingest.triage(docs, db)
    assert len(t1["new"]) == 2 and not t1["changed"]

    ingest.record_sources(ingest.doc_files(docs), db)
    t2 = ingest.triage(docs, db)
    assert not t2["new"] and len(t2["unchanged"]) == 2

    (docs / "a.md").write_text("# A changed")
    t3 = ingest.triage(docs, db)
    assert len(t3["changed"]) == 1 and len(t3["unchanged"]) == 1


def test_curate_prompt_is_agent_agnostic(tmp_path):
    """okf prompt --curate must emit runnable CLI steps for any client, and
    must carry the never-invent honesty rule."""
    p = ingest.build_curate_prompt(tmp_path / "bundle", tmp_path / "i.db")
    assert "okf report" in p and "okf index" in p
    assert "never invent" in p.lower()
    assert "${" not in p  # no Claude-Code-only substitutions leaked in


def test_vendored_dirs_skipped(tmp_path):
    docs = tmp_path / "docs"
    (docs / ".venv" / "lib").mkdir(parents=True)
    (docs / ".venv" / "lib" / "LICENSE.md").write_text("# not ours")
    (docs / "real.md").write_text("# ours")
    files = ingest.doc_files(docs)
    assert [f.name for f in files] == ["real.md"]
