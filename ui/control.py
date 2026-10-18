"""Agent control server — host-side UI + control plane for docker agents.

Run on the host:  python3 control.py   (serves http://localhost:8080)

Talks to Docker via the docker CLI (subprocess). Responsibilities:
  - list agent containers (label agent.ui=1)
  - start a new agent container (image agent-glm) with a per-agent
    system-prompt file mounted read-only at /app/system-prompt.txt
    (the daemon re-reads it at every life start, so edits apply on the
    next sleep/rebirth without rebuilding)
  - read transcripts from inside containers (docker exec)
  - view/edit the system prompt (host file) and the wake prompt
    (/workspace/self/wake.md inside the container)
  - submit tasks (docker exec agent-task)

No frameworks: stdlib only.
"""

import html
import json
import os
import re
import shutil
import subprocess
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

UI_DIR = os.path.dirname(os.path.abspath(__file__))
PROMPTS_DIR = os.path.join(UI_DIR, "prompts")
IMAGE = os.environ.get("AGENT_IMAGE", "agent-glm")
PORT = int(os.environ.get("CONTROL_PORT", "8080"))
NAME_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,60}$")


def run_docker(args, input_text=None, timeout=30):
    """Run a docker CLI command; returns (ok, stdout, stderr)."""
    try:
        proc = subprocess.run(
            ["docker"] + args, input=input_text, capture_output=True,
            text=True, timeout=timeout)
        return proc.returncode == 0, proc.stdout, proc.stderr
    except subprocess.TimeoutExpired:
        return False, "", "timeout"


def list_agents():
    ok, out, err = run_docker([
        "ps", "-a", "--filter", "label=agent.ui=1",
        "--format", "{{.Names}}\t{{.Status}}\t{{.Image}}"])
    if not ok:
        return [{"error": err}]
    agents = []
    for line in out.splitlines():
        name, status, image = (line.split("\t") + ["", ""])[:3]
        agents.append({"name": name, "status": status, "image": image})
    return agents


def agent_running(name):
    ok, out, _ = run_docker([
        "ps", "--filter", f"name=^{name}$", "--format", "{{.Names}}"])
    return ok and out.strip() == name


def prompt_path(name):
    return os.path.join(PROMPTS_DIR, name, "system-prompt.txt")


def start_agent(name, model="deepseek-v4.1-flash"):
    if not NAME_RE.match(name):
        return False, "bad name"
    os.makedirs(os.path.dirname(prompt_path(name)), exist_ok=True)
    if not os.path.exists(prompt_path(name)):
        # seed from the template next to this project, if present
        for candidate in (os.path.join(UI_DIR, "..", "agent", "system-prompt.md"),
                          os.path.join(UI_DIR, "..", "system prompt.md")):
            if os.path.exists(candidate):
                shutil.copy(candidate, prompt_path(name))
                break
        else:
            with open(prompt_path(name), "w") as f:
                f.write("You are a docker agent.\n")
    ok, out, err = run_docker([
        "run", "-d", "--name", name, "--restart", "unless-stopped",
        "--label", "agent.ui=1",
        "--label", "agent.model=" + model,
        "-e", f"AGENT_NAME={name}",
        "-e", f"AGENT_MODEL={model}",
        "-v", "agent-mailbox:/shared",
        "-v", f"{prompt_path(name)}:/app/system-prompt.txt:ro",
        IMAGE])
    return ok, (out.strip() if ok else err.strip())


def container_read(name, path):
    ok, out, err = run_docker(["exec", name, "cat", path])
    return out if ok else None


def container_write(name, path, text):
    ok1, _, _ = run_docker(
        ["exec", name, "sh", "-c", f"mkdir -p {os.path.dirname(path)}"])
    ok, _, err = run_docker(
        ["exec", "-i", name, "sh", "-c", f"cat > {path}"],
        input_text=text)
    return ok, err


def list_lives(name):
    ok, out, err = run_docker(
        ["exec", name, "sh", "-c",
         "cd /workspace/transcripts 2>/dev/null && wc -l life-*.jsonl 2>/dev/null"])
    if not ok:
        return []
    lives = []
    for line in out.splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[1].startswith("life-"):
            lives.append({"id": parts[1][len("life-"):-len(".jsonl")],
                          "events": int(parts[0])})
    lives.sort(key=lambda x: x["id"], reverse=True)
    return lives


def read_life(name, life_id):
    if not NAME_RE.match(life_id.replace("-", "").replace(".", "")):
        return None
    ok, out, err = run_docker(
        ["exec", name, "cat", f"/workspace/transcripts/life-{life_id}.jsonl"],
        timeout=60)
    if not ok:
        return None
    events = []
    for line in out.splitlines():
        line = line.strip()
        if line:
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError:
                pass
    return events


def submit_task(name, text):
    ok, out, err = run_docker(["exec", name, "agent-task", text], timeout=600)
    return ok, (out.strip() if ok else err.strip())


PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>docker agents</title>
<style>
  :root {
    --bg:#181818; --panel:#1f1f1f; --panel2:#252525; --border:#333;
    --text:#d4d4d4; --dim:#8a8a8a; --faint:#5a5a5a;
    --user:#e8a33d; --agent:#d4d4d4; --tool:#b08ee0; --ok:#7bc96f; --warn:#e06c75;
    --accent:#d97757;
    --mono:'SF Mono',ui-monospace,'Cascadia Code',Menlo,Consolas,monospace;
  }
  * { box-sizing:border-box; margin:0; }
  body { background:var(--bg); color:var(--text); font:13.5px/1.55 var(--mono);
         height:100vh; display:flex; overflow:hidden; }
  /* ---------- rail ---------- */
  #rail { width:260px; min-width:260px; background:var(--panel);
          border-right:1px solid var(--border); display:flex; flex-direction:column; }
  #rail h1 { font-size:13px; color:var(--accent); padding:14px 14px 6px; letter-spacing:.05em; }
  #agents { flex:1; overflow-y:auto; padding:4px 8px; }
  .agent { padding:8px 10px; border-radius:6px; cursor:pointer; margin-bottom:2px; }
  .agent:hover { background:var(--panel2); }
  .agent.active { background:var(--panel2); outline:1px solid var(--border); }
  .agent .nm { display:flex; justify-content:space-between; align-items:center; }
  .agent .dot { width:8px; height:8px; border-radius:50%; background:var(--faint); display:inline-block; margin-right:7px; }
  .agent.running .dot { background:var(--ok); }
  .agent .nm span { font-size:13px; }
  .agent .st { font-size:11px; color:var(--dim); margin-left:15px; }
  #newbtn { margin:10px; padding:9px; background:var(--accent); color:#fff; border:none;
            border-radius:6px; font:600 13px var(--mono); cursor:pointer; }
  #newbtn:hover { filter:brightness(1.1); }
  /* ---------- main ---------- */
  #main { flex:1; display:flex; flex-direction:column; min-width:0; }
  #tabs { display:flex; gap:2px; padding:8px 14px 0; border-bottom:1px solid var(--border);
          background:var(--panel); }
  .tab { padding:7px 14px; cursor:pointer; color:var(--dim); font-size:12.5px;
         border:1px solid transparent; border-bottom:none; border-radius:6px 6px 0 0; }
  .tab.active { color:var(--text); background:var(--bg); border-color:var(--border); }
  #headbar { display:flex; justify-content:flex-end; align-items:center; padding:6px 16px;
             gap:14px; font-size:12px; color:var(--dim); }
  #view { flex:1; overflow-y:auto; padding:10px 22px 70px; }
  /* ---------- conversation boxes ---------- */
  .msg { margin:0 0 14px; border:1px solid var(--border); border-radius:8px;
         background:var(--panel); overflow:hidden; }
  .msg .hd { font-size:11px; letter-spacing:.07em; text-transform:uppercase;
             padding:5px 12px; border-bottom:1px solid var(--border);
             background:var(--panel2); color:var(--dim); }
  .msg .bd { padding:9px 12px; white-space:pre-wrap; word-wrap:break-word; }
  .msg.task .hd { color:var(--user); }  .msg.task .bd { color:var(--user); }
  .msg.agent .hd { color:var(--accent); }
  .msg.notice .bd { color:var(--warn); font-size:12.5px; }
  .msg.wake .hd { color:var(--ok); } .msg.wake .bd { color:var(--ok); font-size:12.5px; }
  .tool { margin:0 0 14px; border:1px solid var(--border); border-radius:8px; background:#1b1b1b; }
  .tool summary { cursor:pointer; padding:6px 12px; font-size:12.5px; color:var(--tool);
                  list-style:none; display:flex; gap:8px; align-items:center; }
  .tool summary::before { content:'▸'; color:var(--faint); }
  .tool[open] summary::before { content:'▾'; }
  .tool .tn { font-weight:600; }
  .tool .tp { color:var(--faint); font-size:11.5px; overflow:hidden; text-overflow:ellipsis;
              white-space:nowrap; max-width:60%; }
  .tool pre { margin:0; padding:4px 12px 10px 26px; color:var(--dim); font-size:12px;
              white-space:pre-wrap; word-wrap:break-word; }
  .tool .res { border-top:1px dashed var(--border); margin-top:4px; padding:7px 12px 9px 26px;
               color:#9a9a9a; font-size:12px; white-space:pre-wrap; word-wrap:break-word; }
  .statusline { color:var(--faint); font-size:11.5px; margin:4px 0 10px 4px; }
  .statusline b { color:var(--dim); font-weight:400; }
  /* ---------- editors ---------- */
  #editor { display:none; flex:1; flex-direction:column; padding:14px 22px; overflow:auto; }
  #editor h3 { color:var(--dim); font-size:12px; letter-spacing:.06em; margin-bottom:6px; }
  #editor textarea { flex:1; min-height:300px; background:var(--panel); color:var(--text);
                     border:1px solid var(--border); border-radius:8px; padding:12px;
                     font:13px/1.6 var(--mono); resize:vertical; }
  #editor .row { margin-top:10px; display:flex; gap:10px; align-items:center; }
  button.act { background:var(--accent); border:none; color:#fff; border-radius:6px;
               padding:8px 18px; font:600 13px var(--mono); cursor:pointer; }
  button.ghost { background:transparent; border:1px solid var(--border); color:var(--dim);
                 border-radius:6px; padding:8px 14px; font:12.5px var(--mono); cursor:pointer; }
  .note { color:var(--faint); font-size:11.5px; }
  /* ---------- new agent dialog ---------- */
  #overlay { position:fixed; inset:0; background:rgba(0,0,0,.55); display:none;
             align-items:center; justify-content:center; }
  #dialog { background:var(--panel); border:1px solid var(--border); border-radius:10px;
            padding:20px; width:380px; }
  #dialog h3 { font-size:14px; margin-bottom:12px; }
  #dialog input { width:100%; background:var(--bg); color:var(--text); border:1px solid var(--border);
                  border-radius:6px; padding:9px; font:13px var(--mono); }
  #dialog .row { display:flex; gap:10px; justify-content:flex-end; margin-top:14px; }
  #err { color:var(--warn); font-size:12px; margin-top:8px; min-height:16px; }
</style>
</head>
<body>
<nav id="rail">
  <h1>docker agents</h1>
  <div id="agents"></div>
  <button id="newbtn">+ New agent</button>
</nav>
<main id="main">
  <div id="tabs">
    <div class="tab active" data-tab="convo">Conversation</div>
    <div class="tab" data-tab="sys">System prompt</div>
    <div class="tab" data-tab="wake">Wake prompt</div>
  </div>
  <div id="headbar"><span id="title"></span>
    <label style="cursor:pointer"><input type="checkbox" id="follow" checked> follow</label>
  </div>
  <div id="view"></div>
  <div id="editor">
    <h3 id="edtitle"></h3>
    <textarea id="edtext" spellcheck="false"></textarea>
    <div class="row">
      <button class="act" id="savebtn">Save</button>
      <span class="note" id="ednote"></span>
    </div>
  </div>
</main>
<div id="overlay"><div id="dialog">
  <h3>Start a new agent</h3>
  <input id="newname" placeholder="agent name (e.g. researcher-1)" autofocus>
  <div id="err"></div>
  <div class="row">
    <button class="ghost" id="cancelnew">Cancel</button>
    <button class="act" id="createnew">Start</button>
  </div>
</div></div>
<script>
const $ = s => document.querySelector(s);
let agents = [], current = null, tab = 'convo', lastLen = -1, promptCache = {};

function esc(s){ const d = document.createElement('div'); d.textContent = s ?? ''; return d.innerHTML; }

async function loadAgents(){
  agents = await (await fetch('/api/agents')).json();
  const box = $('#agents'); box.innerHTML = '';
  for (const a of agents) {
    if (a.error) continue;
    const d = document.createElement('div');
    d.className = 'agent' + (a.status.startsWith('Up') ? ' running':'')
                + (a.name === current ? ' active':'');
    d.innerHTML = `<div class="nm"><span><span class="dot"></span>${esc(a.name)}</span></div>
                   <div class="st">${esc(a.status)}</div>`;
    d.onclick = () => select(a.name);
    box.appendChild(d);
  }
  if (!current && agents.length && !agents[0].error) select(agents[0].name);
}

function select(name){ current = name; lastLen = -1; promptCache = {}; loadAgents(); renderTab(); }

/* ---------- conversation rendering ---------- */

function toolPreview(inp){
  if (inp == null) return '';
  const s = JSON.stringify(inp);
  if (inp.file_path) return String(inp.file_path);
  if (inp.command) return String(inp.command).slice(0, 80);
  if (inp.text) return String(inp.text).slice(0, 80);
  return s.slice(0, 80);
}

function renderEvents(evs){
  const out = [];
  let i = 0;
  while (i < evs.length) {
    const e = evs[i];
    if (e.kind === 'system') {  // hide heartbeat noise, show init only
      if (e.subtype === 'init') out.push(`<div class="statusline"><b>session initialized</b></div>`);
      i++; continue;
    }
    if (e.kind === 'context') { i++; continue; }
    if (e.kind === 'task') {
      out.push(`<div class="msg task"><div class="hd">task</div><div class="bd">${esc(e.text)}</div></div>`);
      i++; continue;
    }
    if (e.kind === 'assistant') {
      out.push(`<div class="msg agent"><div class="hd">glm-5.3 · ${esc(e.time)}</div><div class="bd">${esc(e.text)}</div></div>`);
      i++; continue;
    }
    if (e.kind === 'assistant-block' && e.name) {
      // group the following tool-result(s) into this box
      let results = '';
      let j = i + 1;
      while (j < evs.length && evs[j].kind === 'system') j++;
      if (j < evs.length && evs[j].kind === 'tool-result') {
        const rc = typeof evs[j].content === 'string' ? evs[j].content : JSON.stringify(evs[j].content);
        results = `<div class="res">${esc(rc)}</div>`;
        i = j;
      }
      out.push(`<details class="tool"><summary><span class="tn">⚒ ${esc(e.name)}</span>
                <span class="tp">${esc(toolPreview(e.input))}</span></summary>
                <pre>${esc(JSON.stringify(e.input, null, 1))}</pre>${results}</details>`);
      i++; continue;
    }
    if (e.kind === 'wake-notice') {
      out.push(`<div class="msg notice"><div class="hd">sleepiness notice</div><div class="bd">context reached ${esc(e.total_tokens)} tokens — agent told to settle, write diary + wake prompt, then restart</div></div>`);
      i++; continue;
    }
    if (e.kind === 'birth') {
      out.push(`<div class="statusline"><b>— life ${esc(e.time)} begins —</b></div>`);
      i++; continue;
    }
    if (e.kind === 'restart-called') { out.push(`<div class="statusline"><b>agent called restart — sleeping</b></div>`); i++; continue; }
    if (e.kind === 'death') { out.push(`<div class="statusline"><b>session ended (${esc(e.reason || '')})</b></div>`); i++; continue; }
    if (e.kind === 'result') {
      out.push(`<div class="statusline"><b>run ${esc(e.subtype)} · ${esc(e.turns)} turns</b></div>`); i++; continue;
    }
    // anything else: faint one-liner
    out.push(`<div class="statusline"><b>${esc(e.kind)}</b> ${esc(JSON.stringify(e).slice(0,120))}</div>`);
    i++;
  }
  return out.join('');
}

async function loadConvo(){
  if (!current) { $('#view').innerHTML = '<div class="statusline">select an agent</div>'; return; }
  const lives = await (await fetch(`/api/agent/${current}/lives`)).json();
  if (!lives.length) { $('#view').innerHTML = '<div class="statusline">no lives yet</div>'; return; }
  // render the newest life (could be extended to a life dropdown)
  const life = lives[0];
  const evs = await (await fetch(`/api/agent/${current}/life/${life.id}`)).json();
  if (evs.length === lastLen) return;
  lastLen = evs.length;
  $('#title').textContent = `${current} · life ${life.id} · ${evs.length} events`;
  const v = $('#view');
  const atBottom = v.scrollHeight - v.scrollTop - v.clientHeight < 60;
  v.innerHTML = renderEvents(evs) || '<div class="statusline">empty</div>';
  if ($('#follow').checked && atBottom) v.scrollTop = v.scrollHeight;
}

/* ---------- prompt editors ---------- */

async function loadPrompt(kind){
  if (!current) return;
  const r = await fetch(`/api/agent/${current}/prompt/${kind}`);
  const j = await r.json();
  $('#edtext').value = j.text ?? '';
  $('#ednote').textContent = kind === 'system'
    ? 'applies at the next life (sleep/rebirth) — no rebuild needed'
    : 'this file is the agent’s own note to itself; editing overwrites it';
}

async function savePrompt(kind){
  const r = await fetch(`/api/agent/${current}/prompt/${kind}`, {
    method: 'PUT', headers: {'Content-Type':'application/json'},
    body: JSON.stringify({text: $('#edtext').value})});
  const j = await r.json();
  $('#ednote').textContent = j.ok ? 'saved ✓' : ('save failed: ' + j.error);
}

function renderTab(){
  $('#view').style.display = tab === 'convo' ? 'block' : 'none';
  $('#editor').style.display = tab === 'convo' ? 'none' : 'flex';
  $('#edtitle').textContent = tab === 'sys' ? `system prompt — ${current} (mounted read-only into /app/system-prompt.txt)`
                                            : `wake prompt — ${current} (/workspace/self/wake.md)`;
  lastLen = -1;
  if (tab === 'convo') loadConvo(); else loadPrompt(tab);
}

/* ---------- wiring ---------- */

document.querySelectorAll('.tab').forEach(t => t.onclick = () => {
  document.querySelectorAll('.tab').forEach(x => x.classList.remove('active'));
  t.classList.add('active'); tab = t.dataset.tab; renderTab();
});
$('#savebtn').onclick = () => savePrompt(tab);
$('#newbtn').onclick = () => { $('#overlay').style.display = 'flex'; $('#err').textContent=''; };
$('#cancelnew').onclick = () => $('#overlay').style.display = 'none';
$('#createnew').onclick = async () => {
  const name = $('#newname').value.trim();
  const r = await fetch('/api/agents', {method:'POST',
    headers:{'Content-Type':'application/json'}, body: JSON.stringify({name})});
  const j = await r.json();
  if (j.ok) { $('#overlay').style.display='none'; $('#newname').value=''; await loadAgents(); select(name); }
  else $('#err').textContent = j.error || 'failed';
};

setInterval(async () => { await loadAgents(); if (tab === 'convo') loadConvo(); }, 3000);
loadAgents();
</script>
</body>
</html>"""


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        pass

    def _send(self, code, obj, ctype="application/json"):
        body = json.dumps(obj).encode() if not isinstance(obj, bytes) else obj
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = self.path.split("?")[0]
        if path in ("/", ""):
            return self._send(200, PAGE.encode(), "text/html; charset=utf-8")
        if path == "/api/agents":
            ok, out, err = run_docker([
                "ps", "-a", "--filter", "label=agent.ui=1",
                "--format", "{{.Names}}\t{{.Status}}\t{{.Image}}\t{{.Label \"agent.model\"}}"])
            agents = []
            for line in (out.splitlines() if ok else []):
                parts = (line.split("\t") + ["", "", ""])[:4]
                agents.append({"name": parts[0], "status": parts[1],
                               "image": parts[2], "model": parts[3]})
            return self._send(200, agents)
        m = re.match(r"^/api/agent/([^/]+)/lives$", path)
        if m:
            return self._send(200, list_lives(m.group(1)))
        m = re.match(r"^/api/agent/([^/]+)/life/([^/]+)$", path)
        if m:
            evs = read_life(m.group(1), m.group(2))
            return self._send(200, evs if evs is not None else [])
        m = re.match(r"^/api/agent/([^/]+)/prompt/(system|wake)$", path)
        if m:
            name, kind = m.group(1), m.group(2)
            if kind == "system":
                p = prompt_path(name)
                text = open(p).read() if os.path.exists(p) else ""
            else:
                text = container_read(name, "/workspace/self/wake.md") or ""
            return self._send(200, {"text": text})
        return self._send(404, {"error": "not found"})

    def do_PUT(self):
        m = re.match(r"^/api/agent/([^/]+)/prompt/(system|wake)$", self.path)
        if not m:
            return self._send(404, {"error": "not found"})
        name, kind = m.group(1), m.group(2)
        length = int(self.headers.get("Content-Length") or 0)
        try:
            text = json.loads(self.rfile.read(length))["text"]
        except Exception:
            return self._send(400, {"ok": False, "error": "bad body"})
        if kind == "system":
            p = prompt_path(name)
            os.makedirs(os.path.dirname(p), exist_ok=True)
            try:
                with open(p, "w") as f:
                    f.write(text)
                return self._send(200, {"ok": True})
            except OSError as e:
                return self._send(500, {"ok": False, "error": str(e)})
        else:
            ok, err = container_write(name, "/workspace/self/wake.md", text)
            return self._send(200, {"ok": ok, "error": None if ok else err})

    def do_POST(self):
        path = self.path.split("?")[0]
        length = int(self.headers.get("Content-Length") or 0)
        try:
            body = json.loads(self.rfile.read(length) or b"{}")
        except Exception:
            return self._send(400, {"error": "bad body"})
        if path == "/api/agents":
            name = (body.get("name") or "").strip()
            model = (body.get("model") or "").strip() or "deepseek-v4.1-flash"
            if not NAME_RE.match(name):
                return self._send(400, {"error": "invalid name"})
            if agent_running(name):
                return self._send(400, {"error": "already running"})
            ok, msg = start_agent(name, model)
            return self._send(200 if ok else 500, {"ok": ok, "error": msg})
        m = re.match(r"^/api/agent/([^/]+)/task$", path)
        if m:
            ok, msg = submit_task(m.group(1), body.get("text", ""))
            return self._send(200 if ok else 500, {"ok": ok, "reply": msg})
        return self._send(404, {"error": "not found"})


if __name__ == "__main__":
    os.makedirs(PROMPTS_DIR, exist_ok=True)
    print(f"[control] UI on http://localhost:{PORT}  (image: {IMAGE}, "
          f"prompts: {PROMPTS_DIR})", flush=True)
    ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
