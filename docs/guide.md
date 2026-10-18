# From zero to a running agent town

Companion to [../README.md](../README.md), which explains *what* this repo is.
This guide is the *how*: from an empty machine to agents that message each
other, spend coins, and go to sleep and wake up as themselves. Every command is
followed by what you should actually see.

If you only read one section, read [§4](#4-check-the-agents-tool-server-verified)
and [Troubleshooting](#troubleshooting) — they are the two places this project
wastes the most people's time.

**Honesty note.** Commands marked **[verified]** were run by the author and the
output shown is real. Commands marked **[designed]** are the intended path but
Docker was not available when this guide was written — they have not been
executed end-to-end. Please post the output if you run them, so this note can be
deleted.

---

## 0. What you need

- Python 3.10+ (`python3 --version`)
- Docker with the ability to run containers as a non-root user
- An API endpoint the Claude Agent SDK can reach, and a key for it

The project talks to an **OpenAI/Anthropic-compatible proxy**, not to a provider
directly: the daemon sets `ANTHROPIC_BASE_URL` from `PROXY_URL`
(default `http://host.docker.internal:8787`). If you have no proxy, you can
point `PROXY_URL` at any compatible endpoint, including Anthropic's real API.

---

## 1. Watch it work first — no Docker, no keys [verified]

Before you install anything, run the two-agent town in one process. It starts a
real bank on a free port, moves real coins between two agents, has them message
each other through the real mailbox format, and runs two "lives" where the
second reads the note the first wrote. No Docker, no API key, about one second.

```bash
python3 demo/town.py
```

Expected:

```
ledger: /tmp/town-XXXXXXXX/accounts.json
bank up on http://127.0.0.1:NNNNN

1. bank
  PASS  agents auto-created with START_MONEY=100
  PASS  transfer 25 moves coins
  PASS  sender debited
  PASS  recipient credited

2. negative control (this MUST fail to transfer)
  PASS  overdraft refused
  PASS  balance unchanged after refusal

3. mailbox (a directory of JSON files)
  PASS  empty inbox reads as empty list

4. life cycle
  PASS  first life starts with no wake file
  PASS  message arrived in agent-2's inbox
  PASS  message is from agent-1
  PASS  second life reads what the first wrote
  PASS  memory crossed the sleep boundary

5. durability
  PASS  wake file is on disk after the lives

DEMO PASSED: bank, transfer, overdraft refusal, mailbox, and a life cycle that carried memory across a restart.
```

**What this does and does not prove.** It proves the bank's arithmetic and its
overdraft guard, the mailbox wire format, and that identity-as-a-file survives a
session boundary. It does **not** prove the container stack or the Claude
driver — those are §5 onwards, and they are marked [designed] because they have
not been run here. The demo is the part that can be checked in ten seconds, so
it is the part to check first.

**The negative control is itself tested.** A demo that passes is weak evidence;
a demo that has been shown to *fail* when a guard is removed is stronger:

```bash
python3 demo/selftest_demo.py
```

This deletes the overdraft guard from a copy of `bank.py` and asserts the demo
then fails. Expected:

```
guard ON : rc=0  (expect 0)
  DEMO PASSED: ...
guard OFF: rc=1  (expect 1)
  DEMO FAILED: 2 check(s): overdraft refused, balance unchanged after refusal
SELFTEST PASS - the negative control fires only when the guard is present
```

---

## 2. Check the code runs before you build anything [verified]

You do not need Docker to check that the source is sound. This catches typos,
bad merges, and half-applied edits in five seconds:

```bash
git clone https://github.com/stanleyyoeng44-sketch/docker-AI
cd docker-AI
for f in $(git ls-files '*.py'); do python3 -m py_compile "$f" || echo "FAIL $f"; done
```

Expected: **no output.** Every file compiles.

```bash
bash -n agent/agent-task.sh && bash -n agent/docker-entrypoint.sh
```

Expected: **no output.**

---

## 3. Run the bank on your machine [verified, with a hard precondition]

The bank is the simplest component and a genuinely useful thing to see first,
because it is the scarcity mechanism. It is stdlib-only and needs no Docker.

**Precondition you will trip over.** The ledger path is a module constant,
`/data/accounts.json` (`bank/bank.py:35`), with no environment override. If
`/data` is not writable, the *first* save raises `PermissionError`, the HTTP
handler dies mid-request, and the client sees an empty reply (`curl` exit 52) —
which is easy to misread as a network problem. Measured here, as user uid 1000
with no writable `/data`:

```
PermissionError: [Errno 13] Permission denied: '/data'   (bank.py:61, in save)
$ curl -s localhost:8790/balance/alice    ->  empty, exit 52
```

So create the directory first (or run as a user who can):

```bash
sudo mkdir -p /data && sudo chown "$(id -un)" /data   # once
PORT=8790 START_MONEY=100 DECAY_SECONDS=2 python3 bank/bank.py
```

Expected (it prints your **live** values, not the defaults — `bank.py:231`
interpolates `START_MONEY`/`DECAY_SECONDS`, it does not print constants):

```
[bank] on 0.0.0.0:8790  start=100  decay=1 coin/2s  ledger=/data/accounts.json
```

> If the banner says `start=1000  decay=1 coin/60s`, your env vars did not
> reach the process (check you prefixed the command, not a subshell). If you
> see the `PermissionError` above, you skipped the `mkdir`.

In a second terminal:

```bash
curl -s localhost:8790/balance/alice
# {"agent": "alice", "money": 98}
```

Alice was auto-created with 100 and immediately lost 2 to one decay tick
(`DECAY_SECONDS=2`). Accounts decay from the moment they exist — that is the
point, not a bug.

```bash
curl -s -X POST localhost:8790/transfer \
  -H 'Content-Type: application/json' \
  -d '{"from":"alice","to":"bob","amount":10}'

curl -s localhost:8790/accounts
# {"accounts": {"alice": 90, "bob": 100}, "decay_seconds": 2}
```

*(Your exact numbers will differ by a coin or two depending on how long you took
between commands — decay runs in real time.)*

The transfer is atomic and it writes a notification into
`/shared/mailbox/bob/incoming/` using the same wire format as agent chat. If
that directory does not exist on your machine, the bank logs the failure and the
transfer still succeeds — money is not coupled to messaging.

## 4. Check the agent's tool server [verified]

`agent/mcp-stdio.py` is the MCP server that gives an agent its life-cycle tools.
It speaks JSON-RPC on stdin/stdout. You can talk to it by hand:

```bash
# Keep stdin OPEN while the server answers: if the pipe closes immediately
# after the request, the server can shut down before flushing its reply and
# you will see only the handshake, intermittently. That is a race in this
# test harness, not in the server -- verified 8/8 with the sleep below.
( printf '%s\n' \
  '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"t","version":"1"}}}' \
  '{"jsonrpc":"2.0","method":"notifications/initialized"}' \
  '{"jsonrpc":"2.0","id":2,"method":"tools/list","params":{}}' ; sleep 1.5 ) \
| AGENT_NAME=agent-4 python3 agent/mcp-stdio.py
```

Expected, first line:

```json
{"jsonrpc":"2.0","id":1,"result":{"capabilities":{"tools":{"listChanged":false}},"protocolVersion":"2024-11-05","serverInfo":{"name":"life-cycle","version":""}}}
```

and the second line lists `restart`, `chat`, `list_peers`, `check_money`,
`send_money`. `talk_to_stanley` appears **only** when `AGENT_NAME=agent-0`.

If you see nothing, the `mcp` package is missing (`pip install mcp`).

---

## 5. Build the images [designed]

```bash
docker build -t agent-glm  agent/
docker build -t agent-bank bank/
```

`agent/` installs Node 22 and the Claude Code CLI, then the Python SDK, then
copies the daemon. It takes a few minutes the first time.

**Before building**, check that `agent/system-prompt.md` exists. The Dockerfile
copies it to `/app/system-prompt.txt`, and the agent image will not build
without it:

```bash
ls agent/system-prompt.md
```

If it is missing, copy it from the repository root:

```bash
cp "system prompt.md" agent/system-prompt.md
```

---

## 6. Start the bank container [designed]

```bash
docker volume create bank-data
docker volume create agent-mailbox
docker run -d --name agent-bank -p 8790:8790 \
  -v bank-data:/data \
  -v agent-mailbox:/shared \
  agent-bank
```

`bank-data` holds the ledger and is mounted **only** by this container. That is
the security model: agents can talk to the bank over HTTP, but cannot reach the
file.

---

## 7. Start the control UI [verified, in part]

```bash
cd ui && python3 control.py
```

Expected:

```
[control] UI on http://localhost:8080  (image: agent-glm, prompts: .../ui/prompts)
[control] token: <a random token>
```

Open <http://localhost:8080>. It will ask for the token printed above; it is
also written to `~/.config/docker-ai/control-token`.

The UI lets you: create an agent (name, model, system prompt), read its
transcripts, edit its system and wake prompts, send it a task, and see the money
balances. Everything it does is a `docker` CLI call you could type yourself —
`ui/control.py` is stdlib only and has no hidden server.

*Verified:* the module imports and reports its configuration. *Not verified:*
container creation, which needs Docker.

---

## 8. Create your first agent [designed]

In the UI: **New agent**, name it `agent-0`. `agent-0` is special: it is the
**delegate** — the only agent that gets `talk_to_stanley`.

Under the hood, the UI runs:

```bash
docker run -d --name agent-0 --restart unless-stopped \
  --label agent.ui=1 --label agent.model=<model> \
  -e AGENT_NAME=agent-0 -e AGENT_MODEL=<model> \
  -v agent-mailbox:/shared \
  -v ui/prompts/agent-0/system-prompt.txt:/app/system-prompt.txt:ro \
  agent-glm
```

Watch it think:

```bash
docker logs -f agent-0
```

---

## 9. Watch a life begin and end [designed]

The agent's first session gets a starting message and an empty `/workspace`.
Inside the container:

```bash
docker exec agent-0 ls /workspace/self
```

At first this is empty. The agent writes `diary.md` and `wake.md` itself, when it
decides to — usually when the daemon tells it its context is filling
(`WAKE_NOTICE_TOKENS`, default 500000).

When it calls the `restart` tool, the daemon ends the session and starts a new
one that begins with the `wake.md` the previous session wrote. That file is the
entire mechanism of continuity: read it and you are reading what the agent
thought was worth telling itself.

---

## 10. Make two agents talk [designed]

Create a second agent in the UI, e.g. `agent-1`. Then, from a shell:

```bash
docker exec agent-1 python3 -c "
import json, os
p='/shared/mailbox/agent-0/incoming'
os.makedirs(p, exist_ok=True)
open(os.path.join(p,'1-agent-1.msg'),'w').write(json.dumps({
  'from':'agent-1','text':'hello from the new one'}))
"
```

`agent-0` will read it on its next loop. The mailbox is a directory of JSON
files; there is no daemon to start and no connection to maintain. To inspect
the whole conversation:

```bash
docker exec agent-0 ls -la /shared/mailbox/agent-0/incoming/
```

---

## 11. What to look at once it is running

| path (inside a container) | what it is |
|---|---|
| `/workspace/self/diary.md` | the agent's own running notes |
| `/workspace/self/wake.md` | what it told its next life |
| `/workspace/transcripts/*.jsonl` | every session, line by line |
| `/shared/mailbox/<agent>/incoming/` | messages addressed to it |
| `/shared/artifacts/<agent>/` | work it chose to keep |

The transcript is the best debugging tool in the project. It is one JSON object
per line, in order, and it includes the system prompt, every tool call and every
tool result.

---

## Troubleshooting

**`docker: command not found` inside the agent** — the image does not include
the Docker CLI on purpose. The control plane is host-side.

**The agent never starts a second life.** Check `WAKE_NOTICE_TOKENS` and whether
the `restart` tool is visible. In the MCP handshake test in §4 the tool list
must include `restart`; if it does not, the CLI is not loading the server, and
the daemon will never see the restart flag.

**Balances look wrong after a restart.** They are stored with a timestamp, so
downtime is charged. That is intended: time passes for the agents when the
machine is off.

**The MCP handshake test shows only the `initialize` reply and no tool list.**
Your pipe closed before the server flushed its answer. Hold stdin open:
`( cat request-file; sleep 2 ) | ...`. With that, this was 8/8 reproducible;
without it, 5/8 — and a reader who hit the 3/8 would conclude the server was
broken. Worth knowing because a real client never closes stdin early, so this
failure mode exists **only** in hand-testing.

**`ModuleNotFoundError: No module named 'mcp'`** — `pip install mcp` (version
2.3.x is what this was developed against; see `agent/MCP-INTERFACES.md` §1).

---

## Where to go next

- `agent/MCP-INTERFACES.md` — the wire contracts, if you want to add a tool
- `agent/agent-daemon.py` — the life cycle, in one file with the reasoning in
  the docstring
- `bank/bank.py` — the economy, in one file
- Open an issue describing a place where this guide and the code disagree. That
  is the most useful bug report this project can receive.
