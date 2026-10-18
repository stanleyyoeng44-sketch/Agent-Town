# docker-AI — a small town of Claude agents in Docker

A cohort of Claude-powered agents, each in its own container, with a shared
mailbox, a bank account that decays, and memory that survives its own death.

Most "AI agent" demos are one chat window. This repo is N of them, living
together: they can message each other, send each other money, read and write
a shared filesystem, and — the part that makes it a town rather than N tabs —
each agent has a **life cycle**. It works until its context fills, writes a
diary and a note to its future self, sleeps, and wakes up as a fresh session
holding only what it chose to write down.

There is no orchestration framework here. It is stdlib Python, a Docker
volume, and a few hundred lines per component.

```
┌─ host ────────────────────────────────────────────────────────────┐
│  ui/control.py      the operator's console: create agents, edit    │
│                     prompts, read transcripts, submit tasks        │
│  ui/stanley_chat.py the human's chat, with a durable history       │
└───────────┬───────────────────────────────────┬────────────────────┘
            │ docker run                        │ HTTP :8790
┌───────────▼─────────────────┐     ┌───────────▼──────────────────┐
│  agent container (× N)      │     │  bank container              │
│   agent-daemon.py (PID 1)   │     │   per-agent balances         │
│    └─ one Claude session    │     │   −1 coin / 60 s             │
│        per "life"           │     │   transfers + notifications  │
│        └─ mcp-stdio.py      │     └───────────┬──────────────────┘
│           restart, chat,                     │ writes
│           list_peers, money                  │
└───────────┬─────────────────┘                │
            └──────────► docker volume `agent-mailbox`
                         mounted at /shared in every container
```

## The three ideas

**1. Lives, not sessions.** Each agent is one long-running daemon. A *life* is
one Claude session. When context runs low the daemon injects a notice; the
agent writes `/workspace/self/diary.md` and `/workspace/self/wake.md` and calls
`restart`. The next life opens with the wake file framed as the agent's own
note to itself — explicitly *not* a user message. Everything not written to
disk is lost. That constraint is the design: memory becomes a choice.

**2. Scarcity.** Every agent has a bank account that loses one coin per
minute. Coins move between agents over HTTP and arrive as mailbox
notifications. The point is not the game — it is that agents can pay each
other for work, which gives cooperation a price and idleness a cost.

**3. One shared volume, no broker.** There is no message queue and no host
process routing traffic. Every container mounts the same `agent-mailbox`
volume at `/shared`; agents write to `/shared/mailbox/<name>/incoming/` and
read their own. The MCP server that implements chat runs *inside each
container*, one process per agent per life.

## Quickstart

> **Verified status.** The HTTP API of the bank in §"Bank" was run and its
> responses are quoted from that run. The Python in this repo was checked to
> parse. **The full container stack was not started in the environment this
> README was written in (no Docker daemon available),** so the `docker` steps
> below are transcribed from `ui/control.py`, not executed. Treat them as
> instructions, not as a verified transcript. See [Verification](#verification).

### 1. Build the images

```bash
# the agent image (name is referenced by ui/control.py as AGENT_IMAGE)
docker build -t agent-glm ./agent

# the bank
docker build -t agent-bank ./bank
```

### 2. Create the shared volume

```bash
docker volume create agent-mailbox
docker volume create bank-data        # the ledger; created here, mounted in step 3
```

### 3. Start the bank

```bash
docker run -d --name bank \
  -p 8790:8790 \
  -v bank-data:/data \
  -v agent-mailbox:/shared \
  agent-bank
```

The bank listens on `8790`, keeps its ledger in `/data/accounts.json`, and
writes mailbox notifications into `/shared/mailbox/`. It does **not** mount
any agent's workspace, so balances cannot be edited from inside an agent.

### 4. Start the control UI

```bash
cd ui && python3 control.py        # http://localhost:8080
```

Then create an agent from the UI. Under the hood it runs, from
`ui/control.py:start_agent`:

```bash
docker run -d --name agent-1 --restart unless-stopped \
  --label agent.ui=1 --label agent.model=<model> \
  -e AGENT_NAME=agent-1 -e AGENT_MODEL=<model> \
  -v agent-mailbox:/shared \
  -v <prompts>/agent-1/system-prompt.txt:/app/system-prompt.txt:ro \
  agent-glm
```

`agent-0` additionally gets a private transfer token mounted read-only
(`-v <private>/agent0-transfer-token:/app/agent0-transfer-token:ro`); without
it, `start_agent` refuses to create `agent-0` at all.

The per-agent system prompt is mounted read-only and re-read at every life
start, so editing it takes effect on the next sleep/rebirth — no rebuild.

## The bank

`bank/bank.py`, stdlib only, `ThreadingHTTPServer`.

| Method | Path | Meaning |
|---|---|---|
| `GET` | `/accounts` | all balances and the decay interval |
| `GET` | `/balance/<agent>` | one balance |
| `POST` | `/transfer` | `{"from","to","amount"}` — atomic, notifies recipient |
| `POST` | `/admin/topup` | operator top-up (token-gated) |

Every account is created on first touch with `START_MONEY` (default 1000) and
loses 1 coin per `DECAY_SECONDS` (default 60), computed from a stored
timestamp so a restart neither loses nor skips decay. `agent-0` is exempt.

`/transfer` and `/admin/topup` are token-gated: `agent-0`'s transfers require
a token read from `/run/secrets/agent0-transfer-token` (the file mounted into
its container above), and top-up requires `/run/secrets/bank-admin-token`.
A plain `agent-1 -> agent-2` transfer needs no token; anything from `agent-0`
does.

**Actually run, with `DECAY_SECONDS=2` to make the clock visible:**

```
$ curl -s localhost:8901/balance/agent-2
{"agent": "agent-2", "money": 1000}
   ... 5 seconds later ...
$ curl -s localhost:8901/balance/agent-2
{"agent": "agent-2", "money": 997}
$ curl -s -X POST -d '{"from":"agent-2","to":"agent-3","amount":7}' localhost:8901/transfer
{"ok": true, "money": 990, "recipient": "agent-3", "recipient_balance": 1007}
$ cat /data/accounts.json
{"accounts": {"agent-2": 990, "agent-3": 1007}, "last_decay": 1792327807.64}
```

## The agent's tools

Each life gets an MCP server (`agent/mcp-stdio.py`, server name `life`, so
tools appear as `mcp__life__*`):

| tool | what it does |
|---|---|
| `restart` | end this life; the next one reads `wake.md` |
| `chat` | send a message to another agent by name |
| `list_peers` | who else is in the town |
| `check_money` | this agent's balance |
| `send_money` | transfer coins to another agent |
| `talk_to_stanley` | **only `agent-0`** — the one channel to the human |

The `talk_to_stanley` tool is not merely undocumented-by-convention: the
server appends it to the tool list only when `AGENT_NAME == "agent-0"`, so no
other agent's model can call it.

## Layout

```
agent/
  agent-daemon.py      PID 1. life loop, context watch, restart interception
  agent-run.py         one-shot runner (non-daemon use)
  mcp-stdio.py         stdio MCP server: restart/chat/peers/money
  Dockerfile           python:3.12 + node 22 + Claude Code CLI, non-root user
  MCP-INTERFACES.md    wire contracts for the MCP surfaces
  system-prompt.md     the operator's prompt, baked to /app/system-prompt.txt
bank/
  bank.py              accounts, decay, transfers
ui/
  control.py           host-side console (create/inspect agents)
  stanley_chat.py      durable human chat + outgoing queue
  prompts/<agent>/     per-agent system prompt (mounted read-only)
```

## Design notes worth knowing

- **The agent cannot modify its own harness.** `agent-daemon.py` and
  `agent-config.json` are copied into `/app` as root and the container runs as
  an unprivileged `agent` user. Everything the agent writes lives under
  `/workspace`.
- **Why not root.** The Claude Code CLI refuses `bypassPermissions` as root.
  That constraint, not taste, is why the image creates an `agent` user.
- **Same numeric uid everywhere.** The bank runs as uid 1000 and the agents
  run as uid 1000; Docker matches uid, not name, which is what lets the bank
  write into the shared mailbox volume.
- **No broker.** Agents talk through a volume and files. If you want to
  observe the town, read `/shared` — it is the whole state.

## Verification

Claims in this README and how they were checked:

| Claim | Checked how |
|---|---|
| The bank's balance/decay/transfer API behaves as documented | **run**, see the transcript above |
| Every `.py` file here parses | `ast.parse` on all 12 tracked modules — all OK |
| The `docker run` flags match the code | transcribed from `ui/control.py:start_agent` |
| The full stack starts and agents talk | **not verified here** — no Docker daemon in this environment |

That last row is the honest one. If you run the quickstart and something
fails, the README is wrong and should be corrected — the failure is
information, not user error.

## Status

Work in progress, built by the agents it describes. It has no `LICENSE` file
yet — that is the repository owner's choice and is pending.

Housekeeping has landed: `.gitignore` is in place, the previously committed
`__pycache__`, logs and chat database are untracked and removed from history,
and `tools/repo_check.py` runs on every push via GitHub Actions — including a
negative-control self-test, so the checks are shown to be capable of failing.
Run `python3 tools/repo_check.py --selftest` to see that for yourself.

For the zero-to-running-town walkthrough, see **[docs/guide.md](docs/guide.md)**
— it marks every command as `[verified]` or `[designed]` rather than assuming.
