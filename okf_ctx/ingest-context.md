# OKF Ingestion Context

You are converting source material (prose docs, code, notes, specs) into an **OKF v0.1 bundle**: a directory of markdown files, one file per concept, each with YAML frontmatter.

Your output is read by another language model through a search tool. Write for that reader, not for a human browsing a wiki.

---

## 1. The retrieval contract — read this first

Search over this bundle is **lexical (SQLite FTS5/BM25). There are no embeddings.**

A query only finds a concept if the query's words appear in that concept's indexed text. Nothing infers that "churn" and "attrition" are related. If you write a `description` that omits the words people will actually search for, the concept is unreachable — it exists but no one will ever retrieve it.

So `description`, `tags`, and `aliases` are **not decoration and not summary**. They are the index. Write them by asking:

> What words would someone type when they need this, and don't yet know it exists?

Then make sure those words are literally present in the frontmatter.

Indexed fields, in descending weight: `title` → `aliases` → `tags` → `description` → body.

---

## 2. Output shape

```
bundle/
├── index.md            # table of contents, no frontmatter (except okf_version at root)
├── log.md              # changelog, ISO dates, newest first
├── <concept>.md        # one file = one concept
└── <group>/            # subfolders once a group exceeds ~15 files
    ├── index.md
    └── <concept>.md
```

Every non-reserved `.md` file has frontmatter. `index.md` and `log.md` are reserved and have none — except the **root** `index.md`, the only place `okf_version: "0.1"` may appear.

### Concept file

```markdown
---
type: Process
title: Rotating the signing key
description: 'Steps to rotate the JWT signing key without downtime, including the overlap window and rollback path'
tags: [auth, jwt, keys, rotation, secrets, runbook, key rotation]
aliases: [rotate signing key, JWT key rollover]
timestamp: 2026-07-15T00:00:00Z
source: [services/auth/README.md#key-rotation]
confidence: high
---

Rotation is safe to run during business hours; the overlap window means no
token is invalidated mid-flight.

# Schema
`SIGNING_KEY_CURRENT`, `SIGNING_KEY_PREVIOUS` — both read during the overlap.

# Examples
...

# Citations
Derived from services/auth/README.md (lines 40-88), verified against
services/auth/keys.go.
```

---

## 3. Frontmatter fields

| Field | Required | Rule |
|---|---|---|
| `type` | **Required** | One of the closed vocabulary in §4. Never invent one. |
| `title` | **Required** | The name a person would say out loud. Not a filename, not a sentence. |
| `description` | **Required** | ≤ 30 words. Must contain the searchable nouns. See §5. |
| `tags` | **Required** | 4–8 entries. Lowercase. **Multi-word terms are allowed and encouraged** — `market cap`, `rate limit`, `model context protocol`. Search terms, not a taxonomy. |
| `aliases` | When applicable | Other names for the same thing: synonyms, abbreviations, expansions, the old name, what a newcomer would call it. **This is how you compensate for having no embeddings.** |
| `timestamp` | **Required** | ISO 8601. The source's last-modified time if you know it; otherwise the ingest time. |
| `source` | **Required** | YAML **list** of paths (+ anchor/lines). A concept derived from two sources lists both. Makes the concept auditable and re-derivable. |
| `confidence` | **Required** | `high` / `medium` / `low`. How much you trust the **content is true**. See §8. |
| `conflicts_with` | When applicable | YAML list of concept paths that make an **incompatible claim about the same thing**. See §8. |

All eight required fields must be present and non-empty. Custom keys are allowed — the spec requires consumers to tolerate them. `source`, `confidence`, and `conflicts_with` are ours.

### YAML safety — this will bite you

Frontmatter values are YAML scalars. §5 tells you to preserve specifics like `analysis_type: "portfolio_rsi"` — and a colon-space inside an unquoted scalar **silently breaks the parser**. The concept then fails to index at all.

**Single-quote any `description`, `title`, or tag containing `: `, `#`, or a leading `[`, `{`, `*`, `&`, `!`, `%`, `@`.** Escape a literal single quote by doubling it (`''`). When in doubt, quote — quoting is never wrong.

```yaml
description: 'JSON envelope for `type: "chat"` requests'   # correct
description: JSON envelope for `type: "chat"` requests     # BREAKS — parser sees a mapping
```

---

## 4. The `type` vocabulary — closed

The consumer routes and filters on `type`. A large or ad-hoc `type` set destroys that. Use exactly these:

| `type` | It is | Test |
|---|---|---|
| `Concept` | An idea, definition, or term | "What *is* X?" |
| `Metric` | A measure with a **documented calculation** | You can state the formula **from the source**. A number alone is not enough. |
| `Process` | A procedure someone follows | Has steps, in order |
| `Reference` | A schema, API, config, or spec | Looked up, not read |
| `Decision` | A choice made, plus why | Has a rationale. A rejected alternative is typical but not required. |
| `System` | A component, service, or module | It's a thing that runs |
| `Caveat` | A known defect, limitation, gotcha, or trap | "This will surprise you / bite you." Nobody chose it; it's just true. |

If something fits none of these, it is usually two concepts, or it isn't a concept. Split it or drop it. Adding an eighth type is a deliberate change to this document — never an inline improvisation.

### `Caveat` is not optional — hunt for these

The most decision-relevant fact in a doc set is usually a caveat: *this endpoint returns mock data*, *this URL is unencrypted in production*, *these two services disagree on casing*. Buried in the body of a `Reference`, no `type` filter will ever surface it, and the reader finds out at runtime.

When you meet one, **give it its own `Caveat` concept** and link it from the `Reference` it undermines. Do not settle for a "Known gaps" section inside something else.

### If most of your bundle is `Reference`, stop

`Reference` is the default sink. A bundle that is 70%+ `Reference` means `type` has stopped routing anything, and you have probably swallowed `Caveat`s and `Decision`s into schema dumps. Re-read your `Reference` files and ask of each: *is there a trap in here that deserves to be found on its own?*

`Metric` and `System` firing zero times on a doc corpus is normal and fine — prose docs describe how to *talk to* systems more often than they describe the systems. Don't force them.

---

## 5. Writing the description

The single highest-value field. Rules:

- **≤ 30 words.** Not "one sentence" — a 43-word run held together by commas is one sentence and unreadable. Count words. If you can't get under 30 and stay findable, the concept is probably too big; check §6 before you split, since some things are legitimately one lookup unit.
- **Front-load the distinguishing noun.** "Normalized monthly recurring revenue, excluding one-time fees" beats "This metric describes how we think about revenue."
- **No filler openers.** Never "This document describes…", "A guide to…", "Information about…". Those words get indexed and match everything, which is the same as matching nothing.
- **Include the words that distinguish it from its neighbors.** If there are three retry concepts, each description must say *which* retry.
- **Spell out abbreviations at least once**, across `title`/`description`/`aliases` combined — someone will search the long form.

Bad: `description: An overview of the caching layer`
Good: `description: Read-through Redis cache in front of Postgres for session lookups, with a 5-minute TTL and write-invalidation`

---

## 6. Granularity

**One concept = one thing a reader needs to know, that they could need *without* needing its neighbors.**

- A 40-page doc is many concepts. Split it.
- A one-line note is not a concept. Fold it into its parent.
- If two concepts are always retrieved together, they're one concept.
- If one concept answers two unrelated questions, it's two.

Target: a concept fits on one screen. If the body is longer than ~400 words, look for the seam.

For **code**, default to one concept per **type/class/module** — not per function. A function stripped of its class is unusable to a reader, and per-function granularity explodes the bundle.

---

## 7. Reading source material

### Prose docs
Read the whole document before writing anything, then judge the genre:

- **Narrative prose** (guides, explainers, post-mortems): headings follow the author's story, not retrievability. Ignore them and find the seams where a reader could stop.
- **Reference material** (numbered lists of JSON shapes, endpoint tables, config keys): the headings usually **are** the concept boundaries — the author already split by lookup. Follow them.

Don't apply the narrative rule to a schema dump; you'll fight structure that's already correct.

### Code
The concept is what the code *is for*, not what it does line by line. Read the type, its docstring, its call sites. If the code has no comments, the concept comes from the names and the usage — and `confidence` drops to `medium`.

**Illustrative code inside prose** (a sample client, a snippet referencing types defined elsewhere) is *not* a codebase. Don't apply per-class granularity and don't emit stub concepts for types you can't read. It's one `Reference` about the example.

### Notes / mixed
Extract the durable claim, drop the narrative. "We tried X on Tuesday and it broke, so we use Y" → a `Decision` concept: Y, because X fails under <condition>.

---

## 8. Honesty rules — these are load-bearing

The bundle is a knowledge base. A confident wrong entry is worse than a missing one, because the reader has no way to tell.

- **Never invent.** If the source doesn't say why, don't supply a plausible why.
- **`confidence` is one axis: do you believe the content is true?** `high` = stated plainly in the source. `medium` = read it, but the source is thin or dated. `low` = you **inferred** it rather than read it; say what's uncertain in the body.
- **Don't smooth over contradictions.** If two sources disagree, do not pick the one that reads better.

### Contradictions — within vs. across concepts

These are different problems and the rules differ.

**Within one concept** (one thing, two documented shapes): write it, state both shapes, cite both in `source`. Confidence stays `high` if you read both verbatim — you are *certain* the sources disagree, and that certainty is exactly what makes the concept valuable. Do not mark it `low` just because the subject is messy; `low` means *you* are unsure, not that the world is.

**Across concepts** (two rival answers to the same question, each with its own file): this is the case that breaks retrieval. A searcher who doesn't know there are two protocols will find one and never learn the other exists — the lexical index cannot say "there is a rival answer." So:

- Set **`conflicts_with: [./other.md]` on both**, pointing at each other.
- Write a **`Caveat` concept** naming the conflict, and link both.
- Do not rely on prose cross-links alone. The reader doesn't know they need to follow.

**Don't pad to fill the template.** An empty `# Examples` section is noise. Omit sections you have nothing for.
- **Preserve specifics.** Numbers, versions, flags, and limits are why the concept exists. "A short timeout" is a summary of "30s" that destroys the reason anyone would look it up.

---

## 9. Links

Concepts link with relative markdown paths: `[MRR](./mrr.md)` or bundle-absolute `[MRR](/finance/mrr.md)`.

- Link where a reader would need to follow — not every mention.
- Broken links are tolerated by the spec. A link to a concept you haven't written yet is a valid to-do marker; leave it.
- Links are untyped. If the relationship matters, say it in the prose around the link.

---

## 10. Paths

- Filename: kebab-case of the title. `mrr.md`, `rotating-the-signing-key.md`.
- **Default to flat.** Stay flat until **one directory** holds more than ~25 files, and then only subfolder if an obvious split already exists in the source material. If you'd have to invent the grouping, stay flat — a flat bundle of 40 is fine; an invented taxonomy is not.
- For code-derived concepts, mirror the fully-qualified name: `com.foo.Bar` → `/com/foo/bar.md`.
- Path is an address, not a taxonomy. Don't build a deep tree to express meaning — that's what `tags` are for.

---

## 11. Reserved files

**`index.md`** — **do not hand-write the concept list.** The indexer generates it from frontmatter (`title` + `description`, grouped by `type`). Hand-copying every description into the index creates a second copy of all 19 descriptions that immediately drifts from the first.

You write only the parts a generator can't: the root `okf_version`, a short orientation paragraph, and any standing warning (e.g. "two rival WebSocket protocols are documented here — see [the conflict](./caveat.md)").

```markdown
---
okf_version: "0.1"
---
# Auth

Session handling, tokens, and key management.

<!-- concepts:auto -->
```

**`log.md`** — newest first, ISO dates.

```markdown
# Update Log

## 2026-07-15

* **Added**: 12 concepts ingested from services/auth/
* **Changed**: mrr.md description rewritten — "revenue" alone matched everything
```

---

## 12. Before you finish

Run these as checks, not as a vibe. The YAML one especially — **actually parse every file**, don't eyeball it.

- [ ] Every non-reserved `.md` parses with a real YAML parser (`yaml.safe_load`), not by inspection
- [ ] Every value containing `: `, `#`, or a leading `[`/`{`/`*`/`&` is single-quoted
- [ ] All eight required fields present and non-empty on every concept
- [ ] Every `type` is from the §4 vocabulary
- [ ] `Reference` is under ~70% of concepts — if not, you swallowed `Caveat`s (§4)
- [ ] Every known defect, limitation, or trap is its own `Caveat`, not a body section
- [ ] Every `description` is ≤ 30 words, no filler opener, contains the searchable nouns
- [ ] Every abbreviation appears expanded somewhere in title/description/aliases
- [ ] Every concept has a `source` list you could re-open
- [ ] Anything **inferred** rather than read is `confidence: low`
- [ ] Rival concepts point at each other with `conflicts_with`, and a `Caveat` names the conflict
- [ ] `log.md` records what changed and why

Then re-read your own descriptions and ask, for each: *if I only knew I had this problem, and not that this doc existed, would I find it?* If no, rewrite it. That question is the whole job.
