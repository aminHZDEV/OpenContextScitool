"""`okf dashboard --serve` -- the dashboard, plus editing.

Edits go to the MARKDOWN, never to the database: write the file, validate it,
re-index it. That order matters. Writing to `concept` directly would be erased
by the next `okf index --rebuild`, and the markdown is what's in git.

Binds 127.0.0.1 only. This is a local tool with no auth; it must not be exposed.
"""

from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from .dashboard import render_html
from .indexer import index as run_index, split_frontmatter
from .report import collect

BUNDLE: Path = Path("bundle")
DB: Path = Path(".okf/index.db")

EDITOR_JS = """
const modal=document.createElement('div');modal.className='modal';modal.innerHTML=`
 <div class="sheet">
   <div class="shead"><b class="mp"></b>
     <span><span class="msg"></span>
     <button class="save">Save &amp; re-index</button>
     <button class="close">Close</button></span></div>
   <textarea class="ta" spellcheck="false"></textarea>
 </div>`;
document.body.appendChild(modal);
const ta=modal.querySelector('.ta'), mp=modal.querySelector('.mp'),
      msg=modal.querySelector('.msg');
let cur=null;
function open_(p){cur=p;mp.textContent=p;msg.textContent='';ta.value='loading…';
  modal.classList.add('on');
  fetch('/api/raw?path='+encodeURIComponent(p)).then(r=>r.json())
   .then(d=>{ta.value=d.ok?d.text:'ERROR: '+d.error;});}
modal.querySelector('.close').onclick=()=>modal.classList.remove('on');
modal.onclick=e=>{if(e.target===modal)modal.classList.remove('on');};
modal.querySelector('.save').onclick=()=>{
  msg.textContent='saving…';msg.className='msg';
  fetch('/api/save',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({path:cur,text:ta.value})}).then(r=>r.json()).then(d=>{
      if(d.ok){msg.textContent='saved + re-indexed';msg.className='msg ok';
               setTimeout(()=>location.reload(),700);}
      else{msg.textContent=d.error;msg.className='msg bad';}});};
// Every concept path in every table becomes an edit affordance.
document.querySelectorAll('td code').forEach(c=>{
  const t=c.textContent.trim();
  if(!t.endsWith('.md'))return;
  c.classList.add('edit');c.title='click to edit';
  c.onclick=()=>open_(t);});
"""

EDITOR_CSS = """
code.edit{cursor:pointer;border-bottom:1px dashed var(--rule)}
code.edit:hover{color:var(--seq);border-bottom-color:var(--seq)}
.modal{position:fixed;inset:0;background:rgba(0,0,0,.55);display:none;
 align-items:center;justify-content:center;z-index:50;padding:24px}
.modal.on{display:flex}
.sheet{background:var(--surface);border:1px solid var(--ring);border-radius:12px;
 width:min(980px,100%);height:min(80vh,760px);display:flex;flex-direction:column;
 overflow:hidden}
.shead{display:flex;justify-content:space-between;align-items:center;gap:12px;
 padding:12px 16px;border-bottom:1px solid var(--grid)}
.shead b{font:12px/1.4 ui-monospace,monospace;color:var(--ink2);word-break:break-all}
.shead button{background:var(--plane);color:var(--ink);border:1px solid var(--ring);
 border-radius:7px;padding:6px 12px;cursor:pointer;font-size:12px;margin-left:6px}
.shead .save{background:var(--seq);color:#fff;border-color:transparent;font-weight:600}
.msg{font-size:12px;color:var(--muted);margin-right:8px}
.msg.ok{color:var(--good)} .msg.bad{color:var(--crit)}
.ta{flex:1;width:100%;border:0;outline:0;resize:none;padding:16px;
 background:var(--surface);color:var(--ink);
 font:12.5px/1.6 ui-monospace,SFMono-Regular,Menlo,monospace;tab-size:2}
"""


def _safe(path: str) -> Path:
    """Confine to the bundle. `path` arrives from a browser; treat as hostile."""
    root = BUNDLE.resolve()
    p = (root / path.lstrip("/")).resolve()
    if not p.is_relative_to(root) or p.suffix != ".md":
        raise ValueError("path escapes bundle or is not .md")
    return p


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):  # quiet
        pass

    def _send(self, code, body, ctype="application/json"):
        b = body.encode() if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def do_GET(self):
        u = urlparse(self.path)
        if u.path == "/":
            html = render_html(collect(DB), BUNDLE)
            html = html.replace("</style>", EDITOR_CSS + "</style>")
            html = html.replace("</script></body>", EDITOR_JS + "</script></body>")
            return self._send(200, html, "text/html; charset=utf-8")
        if u.path == "/api/raw":
            try:
                p = _safe(parse_qs(u.query).get("path", [""])[0])
                return self._send(200, json.dumps({"ok": True, "text": p.read_text()}))
            except Exception as e:
                return self._send(200, json.dumps({"ok": False, "error": str(e)}))
        self._send(404, json.dumps({"ok": False, "error": "not found"}))

    def do_POST(self):
        if urlparse(self.path).path != "/api/save":
            return self._send(404, json.dumps({"ok": False, "error": "not found"}))
        try:
            n = int(self.headers.get("Content-Length", 0))
            d = json.loads(self.rfile.read(n))
            p = _safe(d["path"])
            text = d["text"]

            # Validate BEFORE writing. A file that fails to parse would index
            # as nothing and silently disappear from search.
            meta, _ = split_frontmatter(text)
            from .indexer import REQUIRED, TYPES
            missing = [f for f in REQUIRED if not meta.get(f)]
            if missing:
                raise ValueError("missing required field(s): " + ", ".join(missing))
            if meta.get("type") not in TYPES:
                raise ValueError(f"type {meta.get('type')!r} not in {sorted(TYPES)}")

            p.write_text(text, encoding="utf-8")
            s = run_index(BUNDLE, DB)  # content_hash: only this file re-indexes
            return self._send(200, json.dumps(
                {"ok": True, "indexed": s["indexed"]}))
        except Exception as e:
            return self._send(200, json.dumps({"ok": False, "error": str(e)}))


def serve(bundle: Path, db: Path, port: int = 8420) -> int:
    global BUNDLE, DB
    BUNDLE, DB = bundle.resolve(), db
    if not db.exists():
        print(f"No index at {db}. Run `okf index` first.")
        return 1
    srv = ThreadingHTTPServer(("127.0.0.1", port), H)
    print(f"okf dashboard  http://127.0.0.1:{port}\n"
          f"  bundle: {BUNDLE}\n"
          f"  click any concept path to edit it. Ctrl-C to stop.")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped")
    return 0
