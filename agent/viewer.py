"""Web UI for the agent daemon's conversation history.

Serves a Claude-Code-style dark UI on port 8080:
  GET /                    — single-page viewer
  GET /api/lives           — list of lives (newest first)
  GET /api/life/<id>       — full event list for one life

Reads /workspace/transcripts/life-*.jsonl only; stdlib only, no state.
"""

import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

TRANSCRIPTS = "/workspace/transcripts"
PORT = int(os.environ.get("VIEWER_PORT", "8080"))

PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>glm-agent — conversation</title>
<style>
  :root {
    --bg: #1a1a1a; --panel: #212121; --border: #333; --text: #d4d4d4;
    --dim: #8a8a8a; --user: #e8a33d; --assistant: #7ec8e3; --tool: #b08ee0;
    --system: #6a9955; --accent: #d97757; --mono: 'SF Mono', ui-monospace, 'Cascadia Code', Menlo, Consolas, monospace;
  }
  * { box-sizing: border-box; margin: 0; }
  body { background: var(--bg); color: var(--text); font: 14px/1.55 var(--mono);
         height: 100vh; display: flex; }
  /* left rail: lives list */
  #rail { width: 250px; min-width: 250px; border-right: 1px solid var(--border);
          background: var(--panel); overflow-y: auto; padding: 12px 0; }
  #rail h1 { font-size: 13px; color: var(--accent); padding: 4px 14px 10px;
             letter-spacing: .04em; }
  .life { padding: 8px 14px; cursor: pointer; color: var(--dim); font-size: 12.5px;
          border-left: 2px solid transparent; }
  .life:hover { color: var(--text); background: #2a2a2a; }
  .life.active { color: var(--text); border-left-color: var(--accent); background: #2a2a2a; }
  .life .id { display: block; }
  .life .meta { font-size: 11px; color: #666; }
  /* main */
  #main { flex: 1; display: flex; flex-direction: column; min-width: 0; }
  #head { padding: 10px 18px; border-bottom: 1px solid var(--border); color: var(--dim);
          font-size: 12px; display: flex; justify-content: space-between; }
  #stream { flex: 1; overflow-y: auto; padding: 14px 18px 60px; }
  .ev { margin: 0 0 10px; }
  .tag { font-size: 10.5px; letter-spacing: .06em; text-transform: uppercase;
         margin-bottom: 3px; }
  .t-user  .tag { color: var(--user); }
  .t-assistant .tag { color: var(--assistant); }
  .t-tool-result .tag, .t-assistant-block .tag { color: var(--tool); }
  .t-system, .t-task, .t-birth, .t-wake-notice, .t-restart-called, .t-death, .t-result
      { color: var(--system); font-size: 12.5px; }
  .bubble { white-space: pre-wrap; word-wrap: break-word; }
  .t-user .bubble { color: var(--user); }
  .t-assistant .bubble { color: var(--text); }
  /* collapsible tool blocks */
  details { border: 1px solid var(--border); border-radius: 6px; background: #1e1e1e; }
  summary { cursor: pointer; padding: 6px 10px; color: var(--tool); font-size: 12.5px;
            list-style: none; }
  summary::before { content: '▸ '; color: var(--dim); }
  details[open] summary::before { content: '▾ '; }
  details pre { padding: 4px 10px 10px 22px; color: var(--dim); font-size: 12px;
                overflow-x: auto; white-space: pre-wrap; }
  #empty { color: var(--dim); padding: 40px; text-align: center; }
</style>
</head>
<body>
  <nav id="rail"><h1>glm-agent</h1><div id="lives"></div></nav>
  <main id="main">
    <div id="head"><span id="title">select a life</span>
      <label><input type="checkbox" id="follow" checked> auto-refresh</label></div>
    <div id="stream"><div id="empty">no life selected</div></div>
  </main>
<script>
const $ = s => document.querySelector(s);
let current = null, known = [];

async function loadLives() {
  const lives = await (await fetch('/api/lives')).json();
  const box = $('#lives'); box.innerHTML = '';
  for (const l of lives) {
    const d = document.createElement('div');
    d.className = 'life' + (l.id === current ? ' active' : '');
    d.innerHTML = `<span class="id">life ${l.id}</span>
                   <span class="meta">${l.events} events</span>`;
    d.onclick = () => { current = l.id; loadLives(); loadLife(l.id); };
    box.appendChild(d);
  }
  if (!current && lives.length) { current = lives[0].id; loadLife(current); loadLives(); }
  return lives;
}

function esc(s) { const d = document.createElement('div'); d.textContent = s ?? ''; return d.innerHTML; }

function render(ev) {
  const cls = 'ev t-' + ev.kind;
  if (ev.kind === 'assistant') {
    return `<div class="${cls}"><div class="tag">agent</div><div class="bubble">${esc(ev.text)}</div></div>`;
  }
  if (ev.kind === 'task') {
    return `<div class="${cls}"><div class="tag">task</div><div class="bubble">${esc(ev.text)}</div></div>`;
  }
  if (ev.kind === 'tool-result') {
    const body = typeof ev.content === 'string' ? ev.content : JSON.stringify(ev.content);
    return `<div class="${cls}"><details><summary>tool result</summary><pre>${esc(body)}</pre></details></div>`;
  }
  if (ev.kind === 'assistant-block' && ev.name) {
    return `<div class="${cls}"><details><summary>tool: ${esc(ev.name)}</summary><pre>${esc(JSON.stringify(ev.input, null, 1))}</pre></details></div>`;
  }
  if (ev.kind === 'assistant-block') {
    return `<div class="${cls}"><details><summary>${esc(ev.block_class || 'block')}</summary><pre>${esc(JSON.stringify(ev, null, 1))}</pre></details></div>`;
  }
  const mini = Object.entries(ev).filter(([k]) => !['kind','time'].includes(k))
      .map(([k,v]) => `${k}: ${typeof v === 'string' ? v : JSON.stringify(v)}`).join('  ·  ');
  return `<div class="${cls}"><span class="tag">${esc(ev.kind)}</span> <span class="meta">${esc(mini)}</span></div>`;
}

let lastCount = -1;
async function loadLife(id) {
  const events = await (await fetch('/api/life/' + id)).json();
  if (events.length === lastCount && $('#stream').childElementCount) return; // unchanged
  lastCount = events.length;
  $('#title').textContent = `life ${id} — ${events.length} events`;
  const s = $('#stream'); s.innerHTML = '';
  for (const ev of events) s.insertAdjacentHTML('beforeend', render(ev));
  if ($('#follow').checked) s.scrollTop = s.scrollHeight;
}

async function tick() {
  const lives = await loadLives();
  if (current) {
    const l = lives.find(x => x.id === current);
    if (l) await loadLife(current); // re-render only when length changed
  }
}
loadLives();
setInterval(tick, 3000);
</script>
</body>
</html>"""


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        pass

    def _send(self, code, body: bytes, ctype="application/json"):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = self.path.split("?")[0]
        if path in ("/", ""):
            return self._send(200, PAGE.encode(), "text/html; charset=utf-8")
        if path == "/api/lives":
            lives = []
            for f in sorted(os.listdir(TRANSCRIPTS)):
                if not f.startswith("life-") or not f.endswith(".jsonl"):
                    continue   # also skips the CURRENT marker file
                life_id = f[len("life-"):-len(".jsonl")]
                n = sum(1 for _ in open(os.path.join(TRANSCRIPTS, f)))
                lives.append({"id": life_id, "events": n})
            lives.sort(key=lambda x: x["id"], reverse=True)
            return self._send(200, json.dumps(lives).encode())
        if path.startswith("/api/life/"):
            life_id = path[len("/api/life/"):]
            safe = life_id.replace("/", "").replace("..", "")
            file = os.path.join(TRANSCRIPTS, f"life-{safe}.jsonl")
            if not os.path.isfile(file):
                return self._send(404, b"[]")
            events = []
            with open(file) as fh:
                for line in fh:
                    line = line.strip()
                    if line:
                        events.append(json.loads(line))
            return self._send(200, json.dumps(events).encode())
        return self._send(404, b"not found")


def serve_forever():
    print(f"[viewer] serving conversation UI on port {PORT}", flush=True)
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()


if __name__ == "__main__":
    serve_forever()
