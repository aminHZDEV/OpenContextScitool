"""`okf dashboard` -- render the report as a self-contained HTML page.

Read-only by design. Concepts live in markdown; this shows you which ones are
failing and where they are, so you can open the file and fix it. Editing
through a UI would mean writing to `concept`, which `okf index --rebuild`
deletes -- the edit would silently vanish.

Colors are the validated reference palette (status + one sequential hue). No
categorical palette is introduced, so there is nothing to re-validate.
"""

from __future__ import annotations

import html
import webbrowser
from pathlib import Path

from .report import collect

CSS = """
:root{color-scheme:light;
 --plane:#f9f9f7; --surface:#fcfcfb; --ink:#0b0b0b; --ink2:#52514e; --muted:#898781;
 --grid:#e1e0d9; --rule:#c3c2b7; --ring:rgba(11,11,11,0.10);
 --good:#0ca30c; --warn:#fab219; --serious:#ec835a; --crit:#d03b3b;
 --seq:#2a78d6; --seq-bg:#cde2fb;}
@media (prefers-color-scheme:dark){:root:where(:not([data-theme=light])){color-scheme:dark;
 --plane:#0d0d0d; --surface:#1a1a19; --ink:#fff; --ink2:#c3c2b7; --muted:#898781;
 --grid:#2c2c2a; --rule:#383835; --ring:rgba(255,255,255,0.10);
 --good:#0ca30c; --warn:#fab219; --serious:#ec835a; --crit:#d03b3b;
 --seq:#3987e5; --seq-bg:#184f95;}}
:root[data-theme=dark]{color-scheme:dark;
 --plane:#0d0d0d; --surface:#1a1a19; --ink:#fff; --ink2:#c3c2b7; --muted:#898781;
 --grid:#2c2c2a; --rule:#383835; --ring:rgba(255,255,255,0.10);
 --seq:#3987e5; --seq-bg:#184f95;}
*{box-sizing:border-box}
body{margin:0;background:var(--plane);color:var(--ink);
 font:14px/1.5 system-ui,-apple-system,"Segoe UI",sans-serif;}
.wrap{max-width:1100px;margin:0 auto;padding:32px 20px 80px}
h1{font-size:20px;margin:0 0 4px} h2{font-size:15px;margin:0 0 2px}
.sub{color:var(--muted);font-size:13px;margin:0 0 28px}
.why{color:var(--ink2);font-size:13px;margin:0 0 10px}
.tiles{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:12px;margin-bottom:32px}
.tile{background:var(--surface);border:1px solid var(--ring);border-radius:10px;padding:14px 16px}
.tile .v{font-size:28px;font-weight:600;letter-spacing:-.02em}
.tile .k{color:var(--muted);font-size:12px;text-transform:uppercase;letter-spacing:.06em;margin-top:2px}
section{background:var(--surface);border:1px solid var(--ring);border-radius:10px;
 padding:18px 20px;margin-bottom:16px;overflow-x:auto}
table{width:100%;border-collapse:collapse;font-size:13px;min-width:520px}
th{text-align:left;font-weight:600;color:var(--ink2);font-size:11px;
 text-transform:uppercase;letter-spacing:.06em;padding:6px 10px 6px 0;
 border-bottom:1px solid var(--rule);white-space:nowrap;cursor:pointer;user-select:none}
th:hover{color:var(--ink)}
td{padding:7px 10px 7px 0;border-bottom:1px solid var(--grid);vertical-align:top}
tr:last-child td{border-bottom:0}
.num{text-align:right;font-variant-numeric:tabular-nums;white-space:nowrap}
code{font:12px/1.4 ui-monospace,SFMono-Regular,Menlo,monospace;color:var(--ink2);
 word-break:break-all}
.pill{display:inline-block;padding:1px 7px;border-radius:99px;font-size:11px;
 font-weight:600;white-space:nowrap}
.p-crit{background:color-mix(in srgb,var(--crit) 16%,transparent);color:var(--crit)}
.p-warn{background:color-mix(in srgb,var(--serious) 20%,transparent);color:var(--serious)}
.p-good{background:color-mix(in srgb,var(--good) 16%,transparent);color:var(--good)}
.p-mute{background:color-mix(in srgb,var(--muted) 16%,transparent);color:var(--muted)}
/* Track is recessive grid, NOT a blue step: a blue track reads as a filled
   bar, making 0% and 100% look alike. The track is chrome; only the fill is data. */
.bar{height:7px;background:var(--grid);border-radius:4px;overflow:hidden;min-width:60px}
.bar>i{display:block;height:100%;background:var(--seq);border-radius:4px}
.empty{color:var(--muted);font-style:italic;padding:8px 0}
.note{background:color-mix(in srgb,var(--warn) 12%,transparent);
 border:1px solid color-mix(in srgb,var(--warn) 40%,transparent);
 border-radius:8px;padding:12px 14px;margin-bottom:24px;font-size:13px;color:var(--ink2)}
.toggle{position:fixed;top:14px;right:14px;background:var(--surface);
 border:1px solid var(--ring);border-radius:8px;padding:6px 11px;cursor:pointer;
 color:var(--ink2);font-size:12px}
"""

JS = """
document.querySelectorAll('th[data-s]').forEach(th=>th.onclick=()=>{
  const tb=th.closest('table').tBodies[0], i=[...th.parentNode.children].indexOf(th);
  const asc=th.dataset.a!=='1'; th.dataset.a=asc?'1':'0';
  [...tb.rows].sort((x,y)=>{
    const a=x.cells[i].dataset.v??x.cells[i].textContent, b=y.cells[i].dataset.v??y.cells[i].textContent;
    const n=parseFloat(a)-parseFloat(b);
    return (isNaN(n)?a.localeCompare(b):n)*(asc?1:-1);
  }).forEach(r=>tb.appendChild(r));
});
const t=document.querySelector('.toggle');
t.onclick=()=>{const d=document.documentElement;
  const now=d.dataset.theme||(matchMedia('(prefers-color-scheme:dark)').matches?'dark':'light');
  d.dataset.theme=now==='dark'?'light':'dark';};
"""


def _e(s) -> str:
    return html.escape(str(s if s is not None else ""))


def _tbl(cols, rows, fmt):
    if not rows:
        return '<p class="empty">Nothing here — good, or no traffic yet.</p>'
    h = "".join(f'<th data-s class="{c[1]}">{c[0]}</th>' for c in cols)
    b = "".join("<tr>" + fmt(r) + "</tr>" for r in rows)
    return f"<table><thead><tr>{h}</tr></thead><tbody>{b}</tbody></table>"


def render_html(r: dict, bundle: Path) -> str:
    t = r["totals"]
    u, st = r["usage"], r["static"]
    nq = t["queries"] or 1
    gaps_n = sum(g["times"] for g in u.get("gaps", []))
    gap_pct = round(100 * gaps_n / nq)

    def sec(title, why, body):
        return f'<section><h2>{title}</h2><p class="why">{why}</p>{body}</section>'

    # Context actually delivered into the model, measured -- not estimated from
    # word counts. ~4 chars/token for English prose; rougher for code-heavy text.
    b = u.get("budget") or {"search_chars": 0, "read_chars": 0}
    total_chars = (b["search_chars"] or 0) + (b["read_chars"] or 0)

    def _k(n):
        return f"{n/1000:.1f}k" if n >= 1000 else str(n)

    # ---- tiles. Bare numbers: no plot, so no hover layer (skill: stat tile).
    tiles = [
        ("Concepts", t["concepts"], ""),
        ("Queries", t["queries"], ""),
        ("Reads", t["reads"], ""),
        ("Context served", f"~{_k(total_chars // 4)} tok", ""),
        ("Gap rate", f"{gap_pct}%", "crit" if gap_pct > 20 else ""),
        ("Reference", f"{st['reference_pct']}%", "crit" if st["reference_pct"] > 70 else ""),
    ]
    tile_h = "".join(
        f'<div class="tile"><div class="v"'
        f'{" style=color:var(--crit)" if c else ""}>{_e(v)}</div>'
        f'<div class="k">{_e(k)}</div></div>' for k, v, c in tiles)

    body = [f'<div class="tiles">{tile_h}</div>']

    if t["queries"] == 0:
        body.append('<div class="note"><b>No traffic logged yet.</b> Every finding '
                    'below the static section needs real usage — connect the MCP '
                    'server and use it. A dashboard over zero queries is a '
                    'dashboard over nothing.</div>')

    body.append('<div class="note"><b>This view is read-only, deliberately.</b> '
                'Concepts live in markdown at <code>' + _e(bundle) + '</code>. '
                'Edit the file, then run <code>okf index</code>. Writing to the '
                'database instead would be erased by the next re-index.</div>')

    # ---- per-concept: offered vs taken. The core join.
    def crow(x):
        offers, reads = x["offers"], x["reads"]
        rate = round(100 * reads / offers) if offers else 0
        pill = ("p-crit" if rate == 0 and offers >= 3 else
                "p-warn" if rate < 34 else "p-good" if rate >= 67 else "p-mute")
        return (f'<td><code>{_e(x["path"])}</code></td>'
                f'<td><span class="pill p-mute">{_e(x["type"] or "?")}</span></td>'
                f'<td class="num" data-v="{offers}">{offers}</td>'
                f'<td class="num" data-v="{reads}">{reads}</td>'
                f'<td data-v="{rate}" style="min-width:110px">'
                f'<div class="bar"><i style="width:{rate}%"></i></div></td>'
                f'<td class="num" data-v="{rate}"><span class="pill {pill}">{rate}%</span></td>'
                f'<td class="num" data-v="{x["avg_rank"] or 0}">{x["avg_rank"] or "-"}</td>')

    body.append(sec(
        "Concepts by call volume",
        "How often search offered each concept, and how often the model actually "
        "opened it. A 0% read rate on many offers means the description promises "
        "what the concept cannot deliver — it is stealing rank from something that could.",
        _tbl([("Concept", ""), ("Type", ""), ("Offers", "num"), ("Reads", "num"),
              ("", ""), ("Read rate", "num"), ("Avg rank", "num")],
             u.get("concept_calls", []), crow)))

    # ---- sessions
    def srow(x):
        z = x["zero_hits"] or 0
        ch = x["chars"] or 0
        return (f'<td><code>{_e(x["id"])}</code></td>'
                f'<td>{_e((x["started_at"] or "")[:16].replace("T", " "))}</td>'
                f'<td>{_e(x["client"] or "-")}</td>'
                f'<td class="num" data-v="{x["queries"]}">{x["queries"]}</td>'
                f'<td class="num" data-v="{x["reads"]}">{x["reads"]}</td>'
                f'<td class="num" data-v="{z}">'
                f'<span class="pill {"p-crit" if z else "p-good"}">{z}</span></td>'
                f'<td class="num" data-v="{ch}">~{_k(ch // 4)} tok</td>')

    body.append(sec(
        "Sessions",
        "One row per connection. Many queries with few reads means that session "
        "was searching and not finding — look at its zero-hit count. "
        "<b>Context</b> is what this tool served into that session, measured.",
        _tbl([("Session", ""), ("Started", ""), ("Client", ""), ("Queries", "num"),
              ("Reads", "num"), ("Zero hits", "num"), ("Context", "num")],
             u.get("sessions", []), srow)))

    # ---- what the AI actually consumed, per concept
    def erow(x):
        ch = x["chars"] or 0
        tr = x["truncs"] or 0
        return (f'<td><code>{_e(x["path"])}</code></td>'
                f'<td class="num" data-v="{x["reads"]}">{x["reads"]}</td>'
                f'<td class="num" data-v="{ch}">{ch:,}</td>'
                f'<td class="num" data-v="{ch}">~{_k(ch // 4)}</td>'
                f'<td class="num" data-v="{ch // max(x["reads"], 1)}">'
                f'~{_k(ch // max(x["reads"], 1) // 4)}</td>'
                f'<td class="num" data-v="{tr}">'
                + (f'<span class="pill p-warn">{tr}</span>' if tr else "-") + "</td>")

    body.append(sec(
        "Context consumed — how much the AI actually read",
        "Characters this tool handed back, <b>measured</b> from the exact payload — "
        "not <code>word_count</code> × reads, which only estimated the concept's size. "
        "Tokens are ~chars/4 for English prose and rougher for code-heavy bodies, so "
        "treat them as a ranking, not a budget. <b>Trunc</b> counts reads that hit the "
        "<code>max_words</code> cap — those concepts are too big and should be split.",
        _tbl([("Concept", ""), ("Reads", "num"), ("Chars", "num"), ("~Tokens", "num"),
              ("~Tok/read", "num"), ("Trunc", "num")], u.get("expensive", []), erow)))

    sc, rc = b["search_chars"] or 0, b["read_chars"] or 0
    if sc or rc:
        tot = sc + rc
        body.append(sec(
            "Where the context budget went",
            "Snippets deliberately made <code>search</code> results bigger to avoid a "
            "second <code>read</code> turn. This is the check on whether that trade paid: "
            "if search dominates and reads are rare, it did.",
            f'<table><tbody>'
            f'<tr><td>search results</td><td class="num">{sc:,} chars</td>'
            f'<td class="num">~{_k(sc//4)} tok</td>'
            f'<td style="min-width:140px"><div class="bar">'
            f'<i style="width:{100*sc//max(tot,1)}%"></i></div></td>'
            f'<td class="num">{100*sc//max(tot,1)}%</td></tr>'
            f'<tr><td>concept bodies</td><td class="num">{rc:,} chars</td>'
            f'<td class="num">~{_k(rc//4)} tok</td>'
            f'<td><div class="bar"><i style="width:{100*rc//max(tot,1)}%"></i></div></td>'
            f'<td class="num">{100*rc//max(tot,1)}%</td></tr>'
            f'</tbody></table>'))

    # ---- failures
    body.append(sec(
        "Knowledge gaps — asked, nothing found",
        "Your write-next list. Each is a question the bundle could not answer at all.",
        _tbl([("Query", ""), ("Times", "num"), ("Last", "")], u.get("gaps", []),
             lambda x: f'<td><code>{_e(x["text"])}</code></td>'
                       f'<td class="num" data-v="{x["times"]}">'
                       f'<span class="pill p-crit">{x["times"]}</span></td>'
                       f'<td>{_e((x["last"] or "")[:16].replace("T", " "))}</td>')))

    body.append(sec(
        "Weak answers — found something, barely",
        "Usually a vocabulary miss: the right concept exists but doesn't contain the "
        "words the searcher used. Fix by adding those words to <code>aliases</code>.",
        _tbl([("Query", ""), ("Top score", "num")], u.get("weak", []),
             lambda x: f'<td><code>{_e(x["text"])}</code></td>'
                       f'<td class="num" data-v="{x["top_score"]}">'
                       f'<span class="pill p-warn">{x["top_score"]}</span></td>')))

    body.append(sec(
        "Insufficient — opened, then searched again",
        "The concept was found and failed to answer. Raw view counts score this as a "
        "success, which is why view counts are the wrong metric.",
        _tbl([("Concept", ""), ("Times", "num")], u.get("insufficient", []),
             lambda x: f'<td><code>{_e(x["path"])}</code></td>'
                       f'<td class="num" data-v="{x["times"]}">'
                       f'<span class="pill p-warn">{x["times"]}</span></td>')))

    body.append(sec(
        "Thin frontmatter",
        "No aliases and a short description or few tags. Search is lexical — these "
        "fields <em>are</em> the index, so thin here means unfindable.",
        _tbl([("Concept", ""), ("Desc chars", "num"), ("Tags", "num")], st.get("thin", []),
             lambda x: f'<td><code>{_e(x["path"])}</code></td>'
                       f'<td class="num" data-v="{x["desc_len"]}">{x["desc_len"]}</td>'
                       f'<td class="num" data-v="{x["n_tags"]}">{x["n_tags"]}</td>')))

    body.append(sec(
        "Dead weight — never offered",
        "Indexed but never surfaced by any search. Unreachable, redundant — or just new.",
        _tbl([("Concept", ""), ("Type", "")], u.get("dead", []),
             lambda x: f'<td><code>{_e(x["path"])}</code></td>'
                       f'<td><span class="pill p-mute">{_e(x["type"])}</span></td>')))

    # ---- raw trail: the evidence behind every finding above
    body.append(sec(
        "Query trail",
        "The raw log. Every finding above is derived from these rows.",
        _tbl([("#", "num"), ("Session", ""), ("When", ""), ("Query", ""),
              ("Hits", "num"), ("Score", "num"), ("Opened", "")],
             u.get("trail", []),
             lambda x: f'<td class="num" data-v="{x["id"]}">{x["id"]}</td>'
                       f'<td><code>{_e((x["session_id"] or "")[:8])}</code></td>'
                       f'<td>{_e((x["ts"] or "")[11:16])}</td>'
                       f'<td><code>{_e(x["text"])}</code></td>'
                       f'<td class="num" data-v="{x["n_results"]}">'
                       f'<span class="pill {"p-crit" if not x["n_results"] else "p-mute"}">'
                       f'{x["n_results"]}</span></td>'
                       f'<td class="num" data-v="{x["top_score"] or 0}">{x["top_score"] or "-"}</td>'
                       f'<td><code>{_e(x["opened"] or "")}</code></td>')))

    return (f'<!doctype html><html lang="en"><head><meta charset="utf-8">'
            f'<meta name="viewport" content="width=device-width,initial-scale=1">'
            f'<title>OKF context dashboard</title><style>{CSS}</style></head><body>'
            f'<button class="toggle">◐ theme</button><div class="wrap">'
            f'<h1>OKF context dashboard</h1>'
            f'<p class="sub">{_e(bundle)}</p>'
            + "".join(body) +
            f'</div><script>{JS}</script></body></html>')


def dashboard(db: Path, bundle: Path, out: Path | None = None, open_it: bool = True) -> int:
    if not db.exists():
        print(f"No index at {db}. Run `okf index` first.")
        return 1
    out = out or db.parent / "dashboard.html"
    out.write_text(render_html(collect(db), bundle), encoding="utf-8")
    print(f"wrote {out}")
    if open_it:
        try:
            webbrowser.open(out.resolve().as_uri())
        except Exception:
            pass
    return 0
