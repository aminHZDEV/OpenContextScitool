"""Find bad context: which concepts mislead, which are dead, what's missing.

Two kinds of finding here, and the difference matters:

  STATIC   - derived from the bundle alone. Works on day one, no traffic needed.
  USAGE    - derived from what agents actually searched and read. Worthless
             until real traffic accumulates, and the most valuable thing here
             once it has.

The usage half rests on one join: `retrieval` (what search OFFERED) against
`read` (what the model TOOK). View counts alone can't tell those apart, and
the gap between them is where the bad context hides.
"""

from __future__ import annotations

import json
from pathlib import Path

from .indexer import connect

# Concepts offered at least this often before "never read" means anything.
MIN_OFFERS = 3


def collect(db: Path) -> dict:
    conn = connect(db)
    q = lambda sql, *a: [dict(r) for r in conn.execute(sql, a).fetchall()]  # noqa: E731

    totals = q("SELECT (SELECT count(*) FROM query) AS queries, "
               "(SELECT count(*) FROM read) AS reads, "
               "(SELECT count(*) FROM concept) AS concepts")[0]

    out: dict = {"totals": totals, "static": {}, "usage": {}}

    # ---------------- static: no traffic required -----------------------

    # A concept nothing links to is reachable only by search. Not a bug, but
    # combined with never-retrieved it means nobody can get there at all.
    out["static"]["orphans"] = q("""
        SELECT c.path, c.title, c.type FROM concept c
        LEFT JOIN edge e ON e.dst = c.path
        WHERE e.dst IS NULL ORDER BY c.path""")

    # Thin frontmatter = thin index. These concepts are nearly unfindable
    # because description/tags/aliases ARE the retrieval layer.
    out["static"]["thin"] = q("""
        SELECT path, title, length(description) AS desc_len,
               (length(tags) - length(replace(tags, char(10), '')) + 1) AS n_tags
        FROM concept
        WHERE aliases = '' AND (length(description) < 60
              OR (length(tags) - length(replace(tags, char(10), '')) + 1) < 4)
        ORDER BY desc_len""")

    types = q("SELECT type, count(*) AS n FROM concept GROUP BY type ORDER BY n DESC")
    out["static"]["types"] = types
    n = totals["concepts"] or 1
    ref = next((t["n"] for t in types if t["type"] == "Reference"), 0)
    out["static"]["reference_pct"] = round(100 * ref / n)

    # ---------------- usage: needs traffic ------------------------------

    # GAP: asked, nothing answered. This is the write-next list.
    out["usage"]["gaps"] = q("""
        SELECT text, count(*) AS times, max(ts) AS last
        FROM query WHERE n_results = 0
        GROUP BY lower(text) ORDER BY times DESC, last DESC LIMIT 20""")

    # WEAK: answered, but badly -- top hit barely scored. Often a vocabulary
    # miss: the right concept exists but doesn't use the searcher's words.
    out["usage"]["weak"] = q("""
        SELECT text, round(top_score, 2) AS top_score, ts
        FROM query WHERE n_results > 0 AND top_score < 1.0
        ORDER BY top_score ASC LIMIT 15""")

    # NOISE: search keeps offering it, the model keeps declining it. The
    # description is writing a cheque the concept can't cash -- it steals rank
    # from something that could have answered.
    out["usage"]["noise"] = q("""
        SELECT rt.concept_path AS path, c.title,
               count(*) AS offered,
               sum(CASE WHEN rd.id IS NOT NULL THEN 1 ELSE 0 END) AS taken
        FROM retrieval rt
        LEFT JOIN read rd ON rd.concept_path = rt.concept_path
                         AND rd.query_id = rt.query_id
        LEFT JOIN concept c ON c.path = rt.concept_path
        GROUP BY rt.concept_path
        HAVING offered >= ? AND taken = 0
        ORDER BY offered DESC LIMIT 15""", MIN_OFFERS)

    # INSUFFICIENT: they opened it, then searched again. The doc was found and
    # failed to answer. Raw view counts score this as a SUCCESS -- which is
    # why view counts are the wrong metric.
    # EXISTS, not JOIN: a JOIN multiplies each read by every later query in the
    # session, so one read in a 12-query session reports as 11 failures.
    # We want "this read was followed by another search", counted once.
    out["usage"]["insufficient"] = q("""
        SELECT rd.concept_path AS path, c.title, count(*) AS times
        FROM read rd
        LEFT JOIN concept c ON c.path = rd.concept_path
        WHERE EXISTS (SELECT 1 FROM query nq
                      WHERE nq.session_id = rd.session_id AND nq.id > rd.query_id)
        GROUP BY rd.concept_path ORDER BY times DESC LIMIT 15""")

    # DEAD: indexed, never even offered. Unreachable or redundant.
    out["usage"]["dead"] = q("""
        SELECT c.path, c.title, c.type FROM concept c
        LEFT JOIN retrieval rt ON rt.concept_path = c.path
        WHERE rt.concept_path IS NULL ORDER BY c.path LIMIT 30""")

    # HOT + STALE: most-read concepts with the oldest timestamps. Highest-risk
    # documents you have -- everyone trusts them, nobody has checked them.
    out["usage"]["hot_stale"] = q("""
        SELECT c.path, c.title, c.timestamp, count(rd.id) AS reads
        FROM concept c JOIN read rd ON rd.concept_path = c.path
        GROUP BY c.path HAVING reads > 0
        ORDER BY reads DESC, c.timestamp ASC LIMIT 10""")

    # EXPENSIVE: how much context each concept actually consumed. response_chars
    # is MEASURED -- the exact payload handed back -- not word_count x reads,
    # which only ever estimated the concept's size. ~4 chars/token for English
    # prose is a rough conversion, and rougher for code-heavy bodies.
    out["usage"]["expensive"] = q("""
        SELECT c.path, c.title, count(rd.id) AS reads,
               sum(rd.response_chars) AS chars,
               sum(rd.truncated) AS truncs
        FROM concept c JOIN read rd ON rd.concept_path = c.path
        GROUP BY c.path ORDER BY chars DESC LIMIT 10""")

    # Where the context budget went overall: search results vs concept bodies.
    # Snippets made search results bigger on purpose -- this is the check that
    # the trade (fewer read() turns) actually paid.
    out["usage"]["budget"] = q("""
        SELECT (SELECT coalesce(sum(response_chars),0) FROM query) AS search_chars,
               (SELECT coalesce(sum(response_chars),0) FROM read)  AS read_chars,
               (SELECT count(*) FROM query WHERE response_chars = 0) AS unmeasured_q""")[0]

    # PER-CONCEPT: the full offered-vs-taken picture, every concept that was
    # ever offered. read_rate is the headline: low = the description oversells.
    out["usage"]["concept_calls"] = q("""
        SELECT rt.concept_path AS path, c.title, c.type,
               count(DISTINCT rt.query_id) AS offers,
               count(DISTINCT rd.id) AS reads,
               count(DISTINCT rt.query_id || '|' || (SELECT session_id FROM query
                     WHERE id = rt.query_id)) AS q_sessions,
               round(avg(rt.rank), 1) AS avg_rank
        FROM retrieval rt
        LEFT JOIN read rd ON rd.concept_path = rt.concept_path
                         AND rd.query_id = rt.query_id
        LEFT JOIN concept c ON c.path = rt.concept_path
        GROUP BY rt.concept_path ORDER BY offers DESC""")

    # PER-SESSION: which session did what. A session with many queries and few
    # reads was searching and not finding.
    out["usage"]["sessions"] = q("""
        SELECT s.id, s.started_at, s.client,
               count(DISTINCT q.id) AS queries,
               sum(CASE WHEN q.n_results = 0 THEN 1 ELSE 0 END) AS zero_hits,
               (SELECT count(*) FROM read r WHERE r.session_id = s.id) AS reads,
               coalesce(sum(q.response_chars), 0)
                 + (SELECT coalesce(sum(r.response_chars),0) FROM read r
                    WHERE r.session_id = s.id) AS chars
        FROM session s LEFT JOIN query q ON q.session_id = s.id
        GROUP BY s.id ORDER BY s.started_at DESC LIMIT 50""")

    # The per-query trail: what was asked, what came back, what got opened.
    # This is the raw evidence behind every finding above.
    out["usage"]["trail"] = q("""
        SELECT q.id, q.session_id, q.ts, q.text, q.n_results,
               round(q.top_score, 2) AS top_score,
               (SELECT group_concat(r.concept_path, ' | ') FROM read r
                WHERE r.query_id = q.id) AS opened
        FROM query q ORDER BY q.id DESC LIMIT 200""")

    conn.close()
    return out


def render(r: dict) -> str:
    t = r["totals"]
    L = [f"OKF context report -- {t['concepts']} concepts, "
         f"{t['queries']} queries, {t['reads']} reads logged\n"]

    def block(title, rows, fmt, why):
        if not rows:
            return
        L.append(f"\n## {title}")
        L.append(f"   {why}")
        for row in rows[:10]:
            L.append("   " + fmt(row))

    L.append("\n" + "=" * 60 + "\n STATIC  (no traffic needed)")
    L.append(f"\n## Type mix   Reference = {r['static']['reference_pct']}%")
    L.append("   " + ", ".join(f"{x['type']}:{x['n']}" for x in r["static"]["types"]))
    if r["static"]["reference_pct"] > 70:
        L.append("   ⚠ over 70% -- type has stopped routing; Caveats likely swallowed")

    block("Thin frontmatter", r["static"]["thin"],
          lambda x: f"{x['path']:<44} desc={x['desc_len']}c tags={x['n_tags']}",
          "description/tags ARE the index. Thin here = unfindable.")
    block("Orphans (nothing links here)", r["static"]["orphans"],
          lambda x: f"{x['path']:<44} {x['type']}",
          "Reachable by search only.")

    L.append("\n" + "=" * 60 + "\n USAGE  (needs real traffic)")
    if t["queries"] == 0:
        L.append("\n   No queries logged yet. Connect the MCP server and use it;\n"
                 "   these findings are worthless until real traffic accumulates.")
        return "\n".join(L)

    block("Knowledge gaps -- asked, nothing found", r["usage"]["gaps"],
          lambda x: f"{x['times']:>3}x  {x['text']!r}",
          "Your write-next list.")
    block("Weak answers -- found something, barely", r["usage"]["weak"],
          lambda x: f"{x['top_score']:>6}  {x['text']!r}",
          "Usually a vocabulary miss: right concept, wrong words. Add aliases.")
    block("Noise -- offered, never taken", r["usage"]["noise"],
          lambda x: f"{x['offered']:>3} offers, 0 reads  {x['path']}",
          "Description promises what the concept can't deliver; steals rank.")
    block("Insufficient -- read, then searched again", r["usage"]["insufficient"],
          lambda x: f"{x['times']:>3}x  {x['path']}",
          "Opened and failed to answer. View counts score this as success.")
    block("Hot + stale", r["usage"]["hot_stale"],
          lambda x: f"{x['reads']:>3} reads  {x['timestamp'][:10]}  {x['path']}",
          "Everyone trusts these; nobody has checked them.")
    b = r["usage"].get("budget") or {}
    sc, rc = b.get("search_chars") or 0, b.get("read_chars") or 0
    if sc or rc:
        tot = sc + rc
        L.append("\n## Context budget -- measured payload, not estimates")
        L.append("   Snippets made search bigger to avoid a second read turn."
                 " If search dominates and reads are rare, the trade paid.")
        L.append(f"   search results  {sc:>8,} chars  ~{sc//4:>6,} tok  {100*sc//tot:>3}%")
        L.append(f"   concept bodies  {rc:>8,} chars  ~{rc//4:>6,} tok  {100*rc//tot:>3}%")

    block("Expensive -- context each concept consumed", r["usage"]["expensive"],
          lambda x: f"{x['chars'] or 0:>7,}c ~{(x['chars'] or 0)//4:>5,}tok  "
                    f"x{x['reads']}"
                    + (f"  [{x['truncs']} truncated]" if x["truncs"] else "")
                    + f"  {x['path']}",
          "Measured chars handed back. ~chars/4 = tokens for prose; rougher for code.")
    block("Dead weight -- never offered", r["usage"]["dead"],
          lambda x: f"{x['path']:<44} {x['type']}",
          "Unreachable or redundant.")

    return "\n".join(L)


def report(db: Path, as_json: bool = False) -> int:
    if not db.exists():
        print(f"No index at {db}. Run `okf index` first.")
        return 1
    r = collect(db)
    print(json.dumps(r, indent=2) if as_json else render(r))
    return 0
