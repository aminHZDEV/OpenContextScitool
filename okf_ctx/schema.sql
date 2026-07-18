-- OKF context index. Derived and disposable: every table here can be rebuilt
-- from the bundle markdown, which is the source of truth. Delete the .db and
-- re-run `index --rebuild` if anything looks wrong.

PRAGMA journal_mode = WAL;

-- Records which bundle this index was built from. The server refuses to serve
-- a database built for a different bundle -- otherwise one stale --db flag
-- silently answers questions about the wrong project.
CREATE TABLE IF NOT EXISTS meta (
    key    TEXT PRIMARY KEY,
    value  TEXT NOT NULL
);

-- What each SOURCE document hashed to when it was last ingested. This is what
-- makes re-ingestion incremental: without it, `okf ingest` re-reads all 50 docs
-- to pick up an edit to one, and you re-pay the whole ingestion pass.
-- Not derived from the bundle -- survives `index --rebuild` deliberately.
CREATE TABLE IF NOT EXISTS source_file (
    path          TEXT PRIMARY KEY,   -- absolute path of the source doc
    content_hash  TEXT NOT NULL,
    ingested_at   TEXT NOT NULL
);

-- ---------------------------------------------------------------- content

CREATE TABLE IF NOT EXISTS concept (
    path          TEXT PRIMARY KEY,   -- bundle-relative, e.g. "auth/rotate-key.md"
    type          TEXT NOT NULL,
    title         TEXT NOT NULL,
    description   TEXT NOT NULL DEFAULT '',
    tags          TEXT NOT NULL DEFAULT '',   -- newline-joined
    aliases       TEXT NOT NULL DEFAULT '',   -- newline-joined
    source        TEXT NOT NULL DEFAULT '',   -- newline-joined
    confidence    TEXT NOT NULL DEFAULT '',
    timestamp     TEXT NOT NULL DEFAULT '',
    body          TEXT NOT NULL DEFAULT '',
    word_count    INTEGER NOT NULL DEFAULT 0, -- proxy for context cost; not tokens
    content_hash  TEXT NOT NULL,              -- sha256 of raw file; skips re-index
    indexed_at    TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS concept_type_idx ON concept(type);

-- Untyped by OKF spec; `kind` is ours. 'link' = markdown link in body,
-- 'conflicts_with' = frontmatter key (§8: rival answers to the same question).
CREATE TABLE IF NOT EXISTS edge (
    src   TEXT NOT NULL,
    dst   TEXT NOT NULL,
    kind  TEXT NOT NULL DEFAULT 'link',
    PRIMARY KEY (src, dst, kind)
);

CREATE INDEX IF NOT EXISTS edge_dst_idx ON edge(dst);

-- Column order is load-bearing: bm25() weights are positional.
-- title > aliases > tags > description > body  (see ingest-context.md §1)
CREATE VIRTUAL TABLE IF NOT EXISTS concept_fts USING fts5(
    title, aliases, tags, description, body,
    path UNINDEXED,
    tokenize = 'porter unicode61'
);

-- ---------------------------------------------------------------- telemetry
-- The join between `retrieval` and `read` is the whole point: it separates
-- what search OFFERED from what the model actually TOOK.

CREATE TABLE IF NOT EXISTS session (
    id          TEXT PRIMARY KEY,
    started_at  TEXT NOT NULL,
    client      TEXT
);

CREATE TABLE IF NOT EXISTS query (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id  TEXT REFERENCES session(id),
    ts          TEXT NOT NULL,
    text        TEXT NOT NULL,
    n_results   INTEGER NOT NULL DEFAULT 0,
    top_score   REAL,                       -- NULL on zero hits => knowledge gap
    -- Characters actually returned into the model's context. MEASURED, not a
    -- word_count proxy: this is the exact payload the tool handed back.
    response_chars INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS retrieval (
    query_id      INTEGER NOT NULL REFERENCES query(id),
    concept_path  TEXT NOT NULL,
    rank          INTEGER NOT NULL,
    score         REAL NOT NULL,
    PRIMARY KEY (query_id, concept_path)
);

CREATE TABLE IF NOT EXISTS read (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id    TEXT REFERENCES session(id),
    ts            TEXT NOT NULL,
    concept_path  TEXT NOT NULL,
    query_id      INTEGER REFERENCES query(id),  -- NULL = opened without searching
    response_chars INTEGER NOT NULL DEFAULT 0,   -- measured payload, as above
    truncated      INTEGER NOT NULL DEFAULT 0    -- hit max_words? => concept too big
);

CREATE INDEX IF NOT EXISTS read_concept_idx ON read(concept_path);
CREATE INDEX IF NOT EXISTS query_session_idx ON query(session_id);