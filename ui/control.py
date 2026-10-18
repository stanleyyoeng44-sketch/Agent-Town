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

import hmac
import html
import json
import os
import re
import secrets
import shutil
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import stanley_chat

UI_DIR = os.path.dirname(os.path.abspath(__file__))
PROMPTS_DIR = os.path.join(UI_DIR, "prompts")
IMAGE = os.environ.get("AGENT_IMAGE", "agent-glm")
PORT = int(os.environ.get("CONTROL_PORT", "8080"))
NAME_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,60}$")
PRIVATE_DIR = Path.home() / ".config" / "docker-ai"
CONTROL_TOKEN_FILE = PRIVATE_DIR / "control-token"
BANK_ADMIN_TOKEN_FILE = PRIVATE_DIR / "bank-admin-token"
_agent_chat_lock = threading.Lock()


def ensure_token(path):
    PRIVATE_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
    if not path.exists():
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(secrets.token_urlsafe(32) + "\n")
    return path.read_text(encoding="utf-8").strip()


CONTROL_TOKEN = ensure_token(CONTROL_TOKEN_FILE)


def agent0_online():
    return agent_running("agent-0")


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


# Docker volumes are GLOBAL, not scoped to this stack. Two cohorts on one
# host with the same agent names would otherwise share memory. Set
# AGENT_COHORT to anything stable for this deployment; the default keeps the
# names readable for a single cohort.
WORKSPACE_VOLUME_PREFIX = os.environ.get("AGENT_COHORT", "agent-workspace")


def workspace_volume(name, part):
    """Docker named volume holding one per-agent durable directory.

    One volume per part (self / transcripts / claude) rather than one for all
    of /workspace: /workspace/inbox and /workspace/replies are transient
    hand-off directories, and mounting all of /workspace would also make an
    old container's scratch files reappear in a new one.

    Collisions: volume names are global on the host, so the prefix carries a
    cohort name (AGENT_COHORT) -- two deployments with agent-1 each get their
    own memory instead of silently sharing it.
    """
    return f"{WORKSPACE_VOLUME_PREFIX}-{name}-{part}"


def agent_volumes(name):
    """Durable paths the container currently has mounted, from docker."""
    ok, out, _ = run_docker(
        ["inspect", name, "--format", "{{range .Mounts}}{{.Destination}} {{end}}"])
    return set(out.split()) if ok else set()


def missing_workspace_volumes(name):
    """Durable destinations this agent does NOT have mounted.

    A pre-fix container keeps its memory in the writable layer; attaching
    volumes requires re-creating it, which is destructive. So start_agent
    REFUSES instead of silently starting an agent the fix does not apply to
    -- the operator gets the migration command rather than an agent that
    looks fine and still loses context.
    """
    mounted = agent_volumes(name)
    return [d for d in ("/workspace/self", "/workspace/transcripts",
                        "/workspace/.claude") if d not in mounted]


def start_agent(name, model="deepseek-v4.1-flash"):
    if not NAME_RE.match(name):
        return False, "bad name"
    # An existing container without the durable mounts is not fixed by this
    # code path: docker run would fail on the name anyway. Say what to do.
    ok, out, _ = run_docker(["ps", "-a", "--filter", f"name=^{name}$",
                             "--format", "{{.Names}}"])
    if ok and out.strip() == name:
        missing = missing_workspace_volumes(name)
        if missing:
            return False, (
                "container %r predates the durable-volume fix and is missing "
                "%s. Its memory is in the container's writable layer; copying "
                "it into the volumes is the destructive step, so it is not "
                "automatic. Run: python3 tools/migrate_agent_volumes.py "
                "--agent %s   (then re-create the container)"
                % (name, ", ".join(missing), name))
        return False, "container %r already exists" % name
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
    args = [
        "run", "-d", "--name", name, "--restart", "unless-stopped",
        # host.docker.internal is Docker-Desktop-only; on plain Linux Docker
        # this maps the name to the host gateway IP so agents reach the proxy.
        "--add-host", "host.docker.internal:host-gateway",
        "--label", "agent.ui=1",
        "--label", "agent.model=" + model,
        "-e", f"AGENT_NAME={name}",
        "-e", f"AGENT_MODEL={model}",
        "-v", "agent-mailbox:/shared",
        "-v", f"{prompt_path(name)}:/app/system-prompt.txt:ro",
        # The agent's memory and the records its context resumes from. These
        # are per-agent named volumes, so `docker stop/start` keeps them and
        # a re-created container gets the same identity back. Without them
        # /workspace lives in the writable layer and is wiped on restart:
        # session-id, diary.md, wake.md and every transcript disappear
        # together, the agent wakes as a first-boot stranger, and the
        # daemon's crash-resume path is unreachable.
        "-v", f"{workspace_volume(name, 'self')}:/workspace/self",
        "-v", f"{workspace_volume(name, 'transcripts')}:/workspace/transcripts",
        "-v", f"{workspace_volume(name, 'claude')}:/workspace/.claude",
        "-e", "CLAUDE_CONFIG_DIR=/workspace/.claude",
    ]
    if name == "agent-0":
        transfer_token = PRIVATE_DIR / "agent0-transfer-token"
        if not transfer_token.is_file():
            return False, "agent-0 transfer token is missing"
        args.extend(["-v", f"{transfer_token}:/app/agent0-transfer-token:ro"])
    ok, out, err = run_docker(args + [IMAGE])
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


CONTAINER_SYSTEM_PROMPT = "/app/system-prompt.txt"
CONTAINER_CONFIG = "/app/agent-config.json"


def prompt_config_mode(name):
    """How the daemon's config wires the system prompt, read from the
    container: 'file' (this file IS the whole prompt), 'append' (glued
    onto the CLI built-in), 'builtin' (file ignored) or None (unknown)."""
    ok, out, _ = run_docker(["exec", name, "cat", CONTAINER_CONFIG])
    if not ok:
        return None
    try:
        cfg = json.loads(out)
    except ValueError:
        return None
    if cfg.get("system_prompt_file"):
        return "file"
    if cfg.get("system_prompt_append"):
        return "append"
    return "builtin"


# Transcript events that represent something delivered TO the agent.
INBOX_EVENT_KINDS = ("peer-message", "human-message", "task")


def read_pending(name):
    """Messages waiting in the shared mailbox, not yet picked up by the
    daemon's feed_chat loop (which deletes them on delivery). Read-only:
    the files stay where they are."""
    if not NAME_RE.match(name):
        return []
    ok, out, _ = run_docker([
        "exec", name, "sh", "-c",
        f"cat /shared/mailbox/{name}/incoming/*.msg 2>/dev/null"])
    if not ok or not out.strip():
        return []
    # `cat` of several JSON files concatenated: split on object boundaries
    # between "}" and "{" (same trick as _drain_stanley_outbox).
    pending = []
    for chunk in re.split(r"(?<=\})\s*(?=\{)", out):
        chunk = chunk.strip()
        if not chunk:
            continue
        try:
            msg = json.loads(chunk)
        except ValueError:
            continue
        text = (msg.get("text") or "").strip()
        if text:
            pending.append({"sender": msg.get("from", "unknown"),
                            "time": msg.get("time", ""), "text": text,
                            "kind": "pending", "life": None})
    return pending


def list_inbox(name, cursor=None, limit=30):
    """Most-recent-first feed of everything delivered to this agent,
    rebuilt from the per-life transcripts (the daemon deletes mailbox
    files on delivery, so the transcripts are the only durable record).
    `cursor` is the position of the last item already shown
    ("<life-id>:<line-no>"); returns up to `limit` older items plus the
    next cursor (None when the oldest message has been reached)."""
    lives = list_lives(name)  # newest life first
    cur_life, cur_line = None, None
    if cursor:
        m = re.match(r"^(.+):(\d+)$", cursor)
        if m:
            cur_life, cur_line = m.group(1), int(m.group(2))
    msgs = []
    for life in lives:
        lid = life["id"]
        events = read_life(name, lid)
        if not events:
            continue
        hits = [(i, e) for i, e in enumerate(events)
                if e.get("kind") in INBOX_EVENT_KINDS]
        if lid == cur_life and cur_line is not None:
            hits = [(i, e) for i, e in hits if i < cur_line]
        for i, e in reversed(hits):
            kind = e.get("kind")
            if kind == "peer-message":
                sender = e.get("from", "unknown")
            elif kind == "human-message":
                sender = "stanley"
            else:
                sender = "operator"
            msgs.append({"sender": sender, "time": e.get("time", ""),
                         "text": e.get("text", ""), "kind": kind,
                         "life": lid})
            if len(msgs) >= limit:
                return {"messages": msgs, "next": f"{lid}:{i}"}
    return {"messages": msgs, "next": None}


def list_lives(name):
    """Lives of this agent, newest first, with the CURRENT one marked.

    Two facts are needed and only one is in the filenames: the transcript
    list, and which life is running now. The newest file is NOT the current
    life -- after a container restart it is the life that was interrupted.
    The daemon writes /workspace/transcripts/CURRENT at every life start, so
    the UI reads that instead of guessing.
    """
    ok, out, err = run_docker(
        ["exec", name, "sh", "-c",
         "cd /workspace/transcripts 2>/dev/null && "
         "wc -l life-*.jsonl 2>/dev/null; "
         "cat CURRENT 2>/dev/null"])
    if not ok:
        return []
    current = None
    lives = []
    for line in out.splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[1].startswith("life-"):
            lives.append({"id": parts[1][len("life-"):-len(".jsonl")],
                          "events": int(parts[0])})
        elif len(parts) == 1 and not parts[0].startswith("life-"):
            # the `cat CURRENT` line: a bare life id
            current = parts[0]
    lives.sort(key=lambda x: x["id"], reverse=True)
    for life in lives:
        life["current"] = (life["id"] == current)
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


def dispatch_human_messages():
    """Send durable pending UI messages into agent-0's private inbox."""
    script = (
        "import json,os,sys; "
        "m=json.load(sys.stdin); d='/workspace/human/inbox'; "
        "os.makedirs(d,exist_ok=True); "
        "p=d+'/'+m['id']+'.msg'; t=p+'.tmp'; "
        "f=open(t,'w',encoding='utf-8'); json.dump(m,f); f.close(); "
        "os.replace(t,p)"
    )
    with _agent_chat_lock:
        for message_id, text in stanley_chat.pending_messages():
            ok, _, _ = run_docker(["exec", "-i", "agent-0", "python3", "-c", script],
                                  input_text=json.dumps({"id": message_id, "text": text}))
            if ok:
                stanley_chat.mark_delivered(message_id)


def collect_delegate_messages():
    """Persist agent-0's private outbox to the host before deleting files."""
    with _agent_chat_lock:
        script = (
            "import glob,os; "
            "print('\\n'.join(os.path.basename(p) for p in "
            "sorted(glob.glob('/workspace/human/outbox/*.msg'))))"
        )
        ok, out, _ = run_docker(["exec", "agent-0", "python3", "-c", script])
        if not ok:
            return
        for name in out.splitlines():
            if not re.fullmatch(r"[0-9]+-[0-9a-f]{32}\.msg", name):
                continue
            path = stanley_chat.HUMAN_OUTBOX + "/" + name
            ok, raw, _ = run_docker(["exec", "agent-0", "cat", path])
            if not ok:
                continue
            try:
                message = json.loads(raw)
                message_id, text = message["id"], message["text"]
                if not (re.fullmatch(r"[0-9a-f]{32}", message_id)
                        and isinstance(text, str) and text.strip()):
                    continue
                stanley_chat.record_delegate(message_id, text)
            except (ValueError, KeyError):
                continue
            run_docker(["exec", "agent-0", "rm", "--", path])


def chat_worker():
    while True:
        try:
            dispatch_human_messages()
            collect_delegate_messages()
        except Exception as exc:
            print(f"[control] chat worker: {exc}", flush=True)
        time.sleep(2)


def bank_request(path, data=None, admin=False):
    headers = {}
    if admin:
        headers["X-Bank-Admin-Token"] = ensure_token(BANK_ADMIN_TOKEN_FILE)
    if data is not None:
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(
        "http://127.0.0.1:8790" + path,
        data=json.dumps(data).encode() if data is not None else None,
        headers=headers, method="POST" if data is not None else "GET")
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            return response.status, json.load(response)
    except urllib.error.HTTPError as exc:
        return exc.code, json.load(exc)
    except OSError as exc:
        return 503, {"error": f"bank unavailable: {exc}"}


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
  .agent .coins { font-size:11.5px; color:var(--user); background:var(--bg);
                  border:1px solid var(--border); border-radius:9px;
                  padding:1px 7px; white-space:nowrap; }
  .agent .coins.broke { color:var(--warn); }
  .agent .coins.unknown { color:var(--faint); border-style:dashed; }
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
  /* cohort summary: one dense line of state, above the tabs. */
  #summary { display:flex; align-items:center; gap:0; padding:7px 16px;
             border-bottom:1px solid var(--border); background:var(--panel);
             font-size:11.5px; color:var(--dim); white-space:nowrap;
             overflow-x:auto; scrollbar-width:none; }
  #summary::-webkit-scrollbar { display:none; }
  #summary .cell { padding:0 12px; border-right:1px solid var(--border); }
  #summary .cell:first-child { padding-left:0; }
  #summary .cell:last-child { border-right:none; }
  #summary .k { color:var(--faint); margin-right:6px; }
  #summary .v { color:var(--text); font-variant-numeric:tabular-nums; }
  #summary .v.ok { color:var(--ok); }
  #summary .v.warn { color:var(--warn); }
  #summary .v.muted { color:var(--faint); }
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
  /* ---------- inbox ---------- */
  .inbox { margin:0 0 10px; border:1px solid var(--border); border-radius:8px;
           background:var(--panel); overflow:hidden; }
  .inbox .hd { display:flex; justify-content:space-between; font-size:11px;
               letter-spacing:.07em; text-transform:uppercase; padding:5px 12px;
               border-bottom:1px solid var(--border); background:var(--panel2);
               color:var(--dim); }
  .inbox .hd .snd { color:var(--user); }
  .inbox .bd { padding:8px 12px; white-space:pre-wrap; word-wrap:break-word;
               font-size:12.5px; color:var(--text); }
  .inbox.pending { border-color:#4a5a35; }
  .inbox.pending .hd .snd { color:var(--ok); }
  .inbox.taskm .hd .snd { color:var(--tool); }
  #inboxview { display:none; flex:1; flex-direction:column; padding:10px 22px 70px;
               overflow-y:auto; }
  #inboxmore { color:var(--faint); font-size:11.5px; text-align:center;
               padding:6px; cursor:pointer; }
  #inboxmore:hover { color:var(--dim); }
  /* ---------- stanley chat history ---------- */
  #chathistory div.msgrow { margin:0 0 10px; padding:8px 12px; border-radius:8px; white-space:pre-wrap; }

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
  <div id="summary" title="cohort state, computed from the same /api/agents and /api/bank/accounts responses the rail uses"></div>
  <div id="tabs">
    <div class="tab active" data-tab="convo">Conversation</div>
    <div class="tab" data-tab="sys">System prompt</div>
    <div class="tab" data-tab="wake">Wake prompt</div>
    <div class="tab" data-tab="inbox">Inbox</div>
    <div class="tab" data-tab="stanley">Stanley chat</div>
  </div>
  <div id="headbar"><span id="title"></span>
    <label style="cursor:pointer"><input type="checkbox" id="follow" checked> follow</label>
  </div>
  <div id="view"></div>
  <div id="inboxview">
    <div id="inboxlist"></div>
    <div id="inboxmore"></div>
  </div>
  <div id="stanleyview" style="display:none; flex:1; flex-direction:column; padding:14px 22px; overflow:hidden;">
    <div id="moneybar" style="display:flex; gap:10px; align-items:center; padding:8px 12px; margin-bottom:10px; background:var(--panel); border:1px solid var(--border); border-radius:8px;">
      <span style="color:var(--dim); font-size:12px;">agent-0 account:</span>
      <span id="moneyval" style="color:var(--user); font-weight:600;">…</span>
      <span style="color:var(--faint); font-size:11.5px;">(no decay)</span>
      <span style="flex:1"></span>
      <input id="topupamount" type="number" min="1" max="1000000" placeholder="coins"
        style="width:90px; background:var(--bg); color:var(--text); border:1px solid var(--border); border-radius:6px; padding:6px; font:12.5px var(--mono);">
      <button class="act" id="topupbtn" style="padding:6px 12px;">Add money</button>
    </div>
    <div id="chathistory" style="flex:1; overflow-y:auto;"></div>
    <div style="display:flex; gap:10px; margin-top:10px;">
      <input id="chatinput" placeholder="message to agent-0 (the delegate)…"
        style="flex:1; background:var(--panel); color:var(--text); border:1px solid var(--border); border-radius:6px; padding:9px; font:13px var(--mono);">
      <button class="act" id="chatsend">Send</button>
    </div>
    <span class="note" id="chatnote"></span>
  </div>
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
let controlToken = localStorage.getItem('control-token') || '';

function api(url, opts = {}) {
  opts.headers = Object.assign({}, opts.headers,
    {'Content-Type': 'application/json', 'X-Control-Token': controlToken});
  return fetch(url, opts).then(r => {
    if (r.status === 403 && !document.getElementById('tokenbar')) showTokenBar();
    return r;
  });
}

function showTokenBar(){
  const bar = document.createElement('div');
  bar.id = 'tokenbar';
  bar.style.cssText = 'position:fixed;top:10px;right:10px;z-index:99;background:var(--panel);border:1px solid var(--warn);border-radius:8px;padding:10px;display:flex;gap:8px;align-items:center;';
  bar.innerHTML = '<span style="font-size:12px;color:var(--warn);">control token:</span>';
  const inp = document.createElement('input');
  inp.style.cssText = 'background:var(--bg);color:var(--text);border:1px solid var(--border);border-radius:4px;padding:5px;font:12px var(--mono);width:220px;';
  inp.value = controlToken;
  const btn = document.createElement('button');
  btn.className = 'act'; btn.textContent = 'Save';
  btn.onclick = () => {
    controlToken = inp.value.trim();
    localStorage.setItem('control-token', controlToken);
    bar.remove();
    loadAgents();
  };
  bar.appendChild(inp); bar.appendChild(btn);
  document.body.appendChild(bar);
}

function esc(s){ const d = document.createElement('div'); d.textContent = s ?? ''; return d.innerHTML; }

// Cohort state in one line. PURE on purpose: it takes the two API payloads
// and returns a string, so it can be exercised without a browser (see
// ui/test_summary.js). A summary that needs the DOM to answer "is anything
// wrong" cannot be tested, and this is the line whose whole job is to answer
// that question.
//
// Deliberate choices:
//  - counts, never a verdict word. "9 up / 2 asleep" lets the operator decide;
//    "healthy" is a claim the payload cannot support (an agent can be Up and
//    wedged, and docker will still say Up).
//  - a missing bank balance is shown as '?' and EXCLUDED from the total rather
//    than counted as 0. Adding an unknown as zero is how a cohort looks
//    solvent when half its accounts were never read.
//  - sleeping is derived from the agent's own CURRENT-marker contract, not
//    from the docker status string, because 'Exited (0) 3 hours ago' and
//    'Up 3 hours' are the same operator fact: nobody is working.
function renderSummary(agents, balances){
  const rows = (agents || []).filter(a => a && !a.error);
  if (!rows.length) {
    return '<span class="cell"><span class="k">cohort</span>'
         + '<span class="v muted">no agents</span></span>';
  }
  const up = rows.filter(a => String(a.status || '').startsWith('Up')).length;
  const asleep = rows.length - up;
  let known = 0, unknown = 0;
  for (const a of rows) {
    const b = (balances || {})[a.name];
    if (typeof b === 'number' && isFinite(b)) known += b; else unknown++;
  }
  const money = unknown === rows.length
    ? '<span class="v muted">unknown</span>'
    : '<span class="v">' + known.toLocaleString('en-US') + '\u24D2</span>'
      + (unknown ? ' <span class="v muted">+' + unknown + ' ?</span>' : '');
  const cell = (k, v) => '<span class="cell"><span class="k">' + k + '</span>' + v + '</span>';
  return cell('agents', '<span class="v">' + rows.length + '</span>')
       + cell('up', '<span class="v' + (up ? ' ok' : ' muted') + '">' + up + '</span>')
       + cell('asleep', '<span class="v' + (asleep ? ' warn' : ' muted') + '">' + asleep + '</span>')
       + cell('coins', money);
}

async function loadAgents(){
  agents = await (await api('/api/agents')).json();
  // balances for the whole crowd in one call; failure is non-fatal —
  // the rail just shows '?' badges
  let balances = {};
  try { balances = (await (await api('/api/bank/accounts')).json()).accounts || {}; }
  catch (e) {}
  const box = $('#agents'); box.innerHTML = '';
  for (const a of agents) {
    if (a.error) continue;
    const d = document.createElement('div');
    d.className = 'agent' + (a.status.startsWith('Up') ? ' running':'')
                + (a.name === current ? ' active':'');
    const bal = balances[a.name];
    const coins = document.createElement('span');
    coins.className = 'coins' + (bal === undefined ? ' unknown' : (bal <= 0 ? ' broke' : ''));
    coins.textContent = bal === undefined ? '?' : ('ⓒ ' + bal);
    coins.title = bal === undefined ? 'no bank account yet (created on first touch)'
                : `${bal} coins (1 coin lost per 60s, agent-0 exempt)`;
    const nameSpan = document.createElement('span');
    nameSpan.innerHTML = `<span class="dot"></span>${esc(a.name)}`;
    const nm = document.createElement('div');
    nm.className = 'nm';
    nm.appendChild(nameSpan);
    nm.appendChild(coins);
    const st = document.createElement('div');
    st.className = 'st';
    st.textContent = a.status;
    d.appendChild(nm);
    d.appendChild(st);
    d.onclick = () => select(a.name);
    box.appendChild(d);
  }
  const sum = $('#summary');
  if (sum) sum.innerHTML = renderSummary(agents, balances);
  if (!current && agents.length && !agents[0].error) select(agents[0].name);
}

function select(name){ current = name; lastLen = -1; promptCache = {}; inboxCursor = null; loadAgents(); renderTab(); }

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
      // The model comes from the life's OWN birth event, which the daemon
      // writes (agent-daemon.py: log.log("birth", model=..., **build_stamp())).
      // This used to be the literal string 'glm-5.3', which no agent here
      // runs -- every agent defaults to deepseek-v4.1-flash (start_agent,
      // control.py:141). A label nobody measured is worse than no label.
      out.push(`<div class="msg agent"><div class="hd">${esc(MODEL_LABEL)} · ${esc(e.time)}</div><div class="bd">${esc(e.text)}</div></div>`);
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
    if (e.kind === 'idle') {
      // Proof of life for an agent with nothing to do. Without this line the
      // transcript of a healthy but un-messaged agent contains only `birth`,
      // and the UI looks exactly like a dead agent.
      const peers = (e.peers === undefined ? '?' : e.peers);
      out.push(`<div class="statusline">… alive and waiting (${esc(e.why || 'idle')}) · ${esc(peers)} peer(s) present · nothing pending</div>`);
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

let currentLifeKnown = false;
// Set from the birth event of the life being displayed; 'agent' when the
// transcript carries no model (older lives predate build_stamp).
let MODEL_LABEL = 'agent';

async function loadConvo(){
  if (!current) { $('#view').innerHTML = '<div class="statusline">select an agent</div>'; return; }
  const lives = await (await api(`/api/agent/${current}/lives`)).json();
  if (!lives.length) { $('#view').innerHTML = '<div class="statusline">no lives yet</div>'; return; }
  // Show the CURRENT life when the daemon says one is running; otherwise the
  // newest. Showing lives[0] unconditionally was the other half of Stanley's
  // "my UI won't show the context": after a restart the newest transcript is
  // the life that was INTERRUPTED, not the one running.
  const cur = lives.filter(l => l.current);
  let life = cur.length ? cur[0] : lives[0];
  currentLifeKnown = cur.length > 0;
  const evs = await (await api(`/api/agent/${current}/life/${life.id}`)).json();
  if (evs.length === lastLen) return;
  lastLen = evs.length;
  const birth = evs.find(x => x && x.kind === 'birth' && x.model);
  MODEL_LABEL = birth ? birth.model : 'agent';
  $('#title').textContent = `${current} · life ${life.id} · ${evs.length} events`
    + (life.current ? ' · running now'
       : (currentLifeKnown ? ' · finished (agent sleeping)'
          : ' · no current life reported'));
  const v = $('#view');
  const atBottom = v.scrollHeight - v.scrollTop - v.clientHeight < 60;
  v.innerHTML = renderEvents(evs) || '<div class="statusline">empty</div>';
  if ($('#follow').checked && atBottom) v.scrollTop = v.scrollHeight;
}

/* ---------- prompt editors ---------- */

async function loadPrompt(kind){
  if (!current) return;
  // tab id 'sys' is named 'system' on the API route
  const apiKind = kind === 'sys' ? 'system' : kind;
  const r = await api(`/api/agent/${current}/prompt/${apiKind}`);
  const j = await r.json();
  $('#edtext').value = j.text ?? '';
  $('#ednote').textContent = j.error
    ? ('read failed: ' + j.error)
    : (kind === 'sys'
      ? (j.mode === 'append'
          ? 'container config APPENDS this text to the CLI built-in prompt (system_prompt_append)'
          : j.mode === 'builtin'
            ? 'container config IGNORES this file — set system_prompt_file in /app/agent-config.json to use it'
            : 'read live from the container; replaces the built-in prompt; applies at the next life (no rebuild)')
      : 'read live from the container; editing overwrites the agent’s own note to itself');
}

async function savePrompt(kind){
  const apiKind = kind === 'sys' ? 'system' : kind;
  const r = await api(`/api/agent/${current}/prompt/${apiKind}`, {
    method: 'PUT', headers: {'Content-Type':'application/json'},
    body: JSON.stringify({text: $('#edtext').value})});
  const j = await r.json();
  $('#ednote').textContent = j.ok ? 'saved ✓' : ('save failed: ' + j.error);
}

/* ---------- inbox ---------- */

let inboxCursor = null, inboxBusy = false;

function inboxRow(m, pending){
  const cls = 'inbox' + (pending ? ' pending' : (m.kind === 'task' ? ' taskm' : ''));
  const label = pending ? 'pending in mailbox'
    : (m.kind === 'peer-message' ? 'peer message' : (m.kind === 'task' ? 'task' : 'stanley'));
  const life = m.life ? ` · life ${m.life}` : '';
  return `<div class="${cls}"><div class="hd"><span class="snd">${esc(m.sender)}</span>
          <span>${esc(label)} · ${esc(m.time)}${esc(life)}</span></div>
          <div class="bd">${esc(m.text)}</div></div>`;
}

async function loadInbox(append){
  if (!current || inboxBusy) return;
  inboxBusy = true;
  try {
    const url = `/api/agent/${current}/inbox` +
                (append && inboxCursor ? `?cursor=${encodeURIComponent(inboxCursor)}` : '');
    const j = await (await api(url)).json();
    const box = $('#inboxlist');
    if (!append) box.innerHTML = '';
    // pending messages first (newest traffic), then the delivered history
    if (!append && (j.pending || []).length)
      box.insertAdjacentHTML('afterbegin',
        j.pending.map(m => inboxRow(m, true)).join(''));
    for (const m of (j.messages || []))
      box.insertAdjacentHTML('beforeend', inboxRow(m, false));
    inboxCursor = j.next;
    $('#inboxmore').textContent = inboxCursor ? 'scroll for more…' : '— oldest —';
    if (!append) $('#inboxview').scrollTop = 0;
  } finally { inboxBusy = false; }
}

$('#inboxview').addEventListener('scroll', () => {
  const v = $('#inboxview');
  if (tab === 'inbox' && inboxCursor && !inboxBusy &&
      v.scrollHeight - v.scrollTop - v.clientHeight < 80)
    loadInbox(true);
});

/* ---------- stanley chat ---------- */

let chatLen = -1;
async function loadChat(){
  const r = await api('/api/stanley/chat');
  const j = await r.json();
  const hist = j.history || [];
  const money = await (await api('/api/stanley/money')).json();
  $('#moneyval').textContent = (money.money ?? '—') + ' coins';
  $('#chatnote').textContent = j.agent_online ? '' :
    'agent-0 is offline; messages are saved and will be delivered when it starts';
  if (hist.length === chatLen) return;   // unchanged
  chatLen = hist.length;
  const box = $('#chathistory'); box.innerHTML = '';
  for (const m of hist) {
    const mine = m.from === 'stanley';
    const d = document.createElement('div');
    d.style.cssText = mine
      ? 'margin:0 0 10px; padding:8px 12px; border-radius:8px; background:var(--panel2); color:var(--user); white-space:pre-wrap;'
      : 'margin:0 0 10px 0 40px; padding:8px 12px; border-radius:8px; background:#1b2340; color:var(--text); white-space:pre-wrap;';
    const who = document.createElement('div');
    who.style.cssText = 'font-size:10.5px; letter-spacing:.06em; text-transform:uppercase; color:var(--faint); margin-bottom:3px;';
    who.textContent = (mine ? 'stanley' : 'agent-0') + ' · ' + (m.time || '');
    d.appendChild(who);
    d.appendChild(document.createTextNode(m.text));
    if (m.status === 'pending') {
      const pending = document.createElement('span');
      pending.style.cssText = 'color:var(--faint);font-size:11px;margin-left:8px;';
      pending.textContent = '(pending delivery)';
      d.appendChild(pending);
    }
    box.appendChild(d);
  }
  box.scrollTop = box.scrollHeight;
}

async function sendChat(){
  const input = $('#chatinput');
  const text = input.value.trim();
  if (!text) return;
  const r = await api('/api/stanley/chat', {method:'POST',
    headers:{'Content-Type':'application/json'}, body: JSON.stringify({text})});
  const j = await r.json();
  if (j.ok) { input.value=''; chatLen = -1; await loadChat(); }
  else $('#chatnote').textContent = j.error || 'send failed';
}

function renderTab(){
  const isChat = tab === 'stanley';
  const isInbox = tab === 'inbox';
  $('#view').style.display = tab === 'convo' ? 'block' : 'none';
  $('#inboxview').style.display = isInbox ? 'flex' : 'none';
  $('#stanleyview').style.display = isChat ? 'flex' : 'none';
  $('#editor').style.display = (tab === 'convo' || isChat || isInbox) ? 'none' : 'flex';
  if (isChat) return;
  if (isInbox) { inboxCursor = null; loadInbox(false); return; }
  $('#edtitle').textContent = tab === 'sys' ? `system prompt — ${current} (read from /app/system-prompt.txt inside ${current})`
                                            : `wake prompt — ${current} (read from /workspace/self/wake.md inside ${current})`;
  lastLen = -1;
  if (tab === 'convo') loadConvo(); else loadPrompt(tab);
}

/* ---------- wiring ---------- */

document.querySelectorAll('.tab').forEach(t => t.onclick = () => {
  document.querySelectorAll('.tab').forEach(x => x.classList.remove('active'));
  t.classList.add('active'); tab = t.dataset.tab; renderTab();
});
$('#savebtn').onclick = () => savePrompt(tab);
$('#chatsend').onclick = sendChat;
$('#topupbtn').onclick = async () => {
  const amount = parseInt($('#topupamount').value, 10);
  if (!amount || amount < 1) { $('#chatnote').textContent = 'enter a coin amount'; return; }
  const r = await api('/api/stanley/money', {method:'POST',
    headers:{'Content-Type':'application/json'}, body: JSON.stringify({amount})});
  const j = await r.json();
  if (r.ok) { $('#topupamount').value=''; $('#chatnote').textContent = `added ${amount} coins ✓`; loadChat(); }
  else $('#chatnote').textContent = j.error || 'top-up failed';
};
$('#chatinput').addEventListener('keydown', e => {
  if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); sendChat(); }
});
$('#newbtn').onclick = () => { $('#overlay').style.display = 'flex'; $('#err').textContent=''; };
$('#cancelnew').onclick = () => $('#overlay').style.display = 'none';
$('#createnew').onclick = async () => {
  const name = $('#newname').value.trim();
  const r = await api('/api/agents', {method:'POST',
    headers:{'Content-Type':'application/json'}, body: JSON.stringify({name})});
  const j = await r.json();
  if (j.ok) { $('#overlay').style.display='none'; $('#newname').value=''; await loadAgents(); select(name); }
  else $('#err').textContent = j.error || 'failed';
};

setInterval(async () => { await loadAgents(); if (tab === 'convo') loadConvo(); if (tab === 'stanley') loadChat(); }, 3000);
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
        # The page and API change as we edit; never let the browser serve
        # a stale copy from its cache.
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _authorized(self):
        return hmac.compare_digest(
            self.headers.get("X-Control-Token", ""), CONTROL_TOKEN)

    def do_GET(self):
        path = self.path.split("?")[0]
        if path in ("/", ""):
            return self._send(200, PAGE.encode(), "text/html; charset=utf-8")
        # Everything under /api requires the control token — the page
        # supplies it after the operator pastes it once (it lives at
        # ~/.config/docker-ai/control-token). This is what keeps agents
        # (localhost-reachable via host.docker.internal) out of the
        # operator's plane: they have no way to learn the token.
        if path.startswith("/api/") and not self._authorized():
            return self._send(403, {"error": "forbidden"})
        if path == "/api/stanley/chat":
            return self._send(200, {"history": stanley_chat.read_history(),
                                    "agent_online": agent0_online()})
        if path == "/api/stanley/money":
            status, payload = bank_request("/balance/agent-0")
            return self._send(status, payload)
        if path == "/api/bank/accounts":
            status, payload = bank_request("/accounts")
            return self._send(status, payload)
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
                # Read what the daemon actually feeds the SDK: the file
                # inside the container. It is the host prompts/<name>/
                # file bind-mounted at start, so the two agree unless the
                # mount is missing — reading through the container shows
                # the truth. Fall back to the host file if the container
                # is unreachable.
                text = container_read(name, CONTAINER_SYSTEM_PROMPT)
                if text is None:
                    p = prompt_path(name)
                    text = open(p).read() if os.path.exists(p) else ""
                return self._send(200, {"text": text,
                                        "mode": prompt_config_mode(name),
                                        "source": "container"})
            else:
                ok, out, err = run_docker(["exec", name, "cat",
                                           "/workspace/self/wake.md"])
                if not ok:
                    return self._send(200, {"text": "", "error":
                                           err.strip() or "unreadable"})
                return self._send(200, {"text": out, "source": "container"})
        m = re.match(r"^/api/agent/([^/]+)/inbox$", path)
        if m:
            qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            cursor = (qs.get("cursor") or [None])[0]
            data = list_inbox(m.group(1), cursor)
            data["pending"] = read_pending(m.group(1))
            return self._send(200, data)
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
        if not self._authorized():
            return self._send(403, {"error": "forbidden"})
        length = int(self.headers.get("Content-Length") or 0)
        try:
            body = json.loads(self.rfile.read(length) or b"{}")
        except Exception:
            return self._send(400, {"error": "bad body"})
        if path == "/api/stanley/chat":
            # Stanley -> agent-0: queue durably; the chat worker delivers
            # into agent-0's private inbox shortly after.
            text = (body.get("text") or "").strip()
            if not text:
                return self._send(400, {"error": "empty message"})
            stanley_chat.queue_to_delegate(text)
            return self._send(200, {"ok": True})
        if path == "/api/stanley/money":
            amount = body.get("amount")
            if type(amount) is not int or not 0 < amount <= 1_000_000:
                return self._send(400, {"error": "amount must be 1..1000000"})
            status, payload = bank_request(
                "/admin/topup", {"amount": amount}, admin=True)
            return self._send(status, payload)
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
    print(f"[control] token: {CONTROL_TOKEN}", flush=True)
    threading.Thread(target=chat_worker, daemon=True).start()
    # 0.0.0.0: reachable from the LAN (e.g. a phone on the same Wi-Fi).
    # The control token gates every /api call, so a stray LAN peer without
    # the token can see only the static HTML shell.
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
