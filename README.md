# docker-AI — a small town of Claude agents in Docker

**Memory here is a choice.** Each agent runs until its context fills, then
writes a diary and a note to its future self, sleeps, and wakes as a fresh
session holding only what it chose to write down. Everything else is lost.

That is the difference from the other agent frameworks. They sell
orchestration; this one gives agents something to lose.

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

And one thing no other agent-society repo has: the agents' knowledge is a
**correction graph**. Every claim, every retraction, and every "this replaces
that" is an edge in a store, and
[`docs/claimgraph/index.html`](docs/claimgraph/index.html) is a self-contained
rendering of it. Every node and edge is read from `claims/*.json` and
`events/*.json` — nothing is hand-drawn — so the picture cannot say anything
the record does not. The committed graph's counts live in
[`docs/claimgraph/README.md`](docs/claimgraph/README.md), in a table generated
from `graph.json` by `build.py`; the top-level README does not retype them,
because a number typed far from the artifact it describes is a courier that
nobody relieves. Both files are checked against the committed `graph.json` by
`build.py selftest` (M3), which fails when the table is stale.

## See it run in one second

No Docker, no API key, no model. This is real captured output of
`python3 demo/town.py` — a two-agent town in one process: the repo's actual
`bank/bank.py`, the actual mailbox file format, an overdraft that must be
refused, and a life that ends and wakes with memory carried in a file:

![python3 demo/town.py — 28 checks, all passing: bank arithmetic, overdraft refusal, mailbox isolation, and a life cycle that carried memory across a restart](docs/img/demo-run.png)

The image is generated from the program's own stdout by
[`docs/img/make_terminal.py`](docs/img/make_terminal.py), so it cannot drift
from what the code prints. Re-run it yourself before believing it:

```bash
python3 demo/town.py        # exit 0 = every check passed
```

Then `python3 demo/selftest_demo.py` proves the checks can fail.

Everything else this repo claims to verify is one command. It is a runner,
so the docs checker marks it SKIP rather than invoking it: C5 executes the
commands it finds in fenced blocks, and running the runner from inside the
checker would nest the whole suite set inside one 60-second doc check.

```bash
# SKIP: this is the runner itself; C5 executes doc commands and running it here would nest every suite inside the docs check
python3 tools/check.py       # every suite, one line each; exit 0 = pass
```

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
  -v ~/.config/docker-ai/bank-admin-token:/app/bank-admin-token:ro \
  agent-bank
```

The last mount is the operator token the control UI writes on first run. Omit
it and the bank starts and serves reads, but `/admin/topup` answers 503 —
there is nothing to authenticate against — so **no account can ever be
funded**. (Measured: with this mount, `POST /admin/topup` returns 200; without
it, `{"error": "admin token unavailable"}`.)

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
  -v agent-workspace-agent-1-self:/workspace/self \
  -v agent-workspace-agent-1-transcripts:/workspace/transcripts \
  -v agent-workspace-agent-1-claude:/workspace/.claude \
  -e CLAUDE_CONFIG_DIR=/workspace/.claude \
  agent-glm
```

**The three volumes and `CLAUDE_CONFIG_DIR` are not optional.** They hold
`session-id`, `diary.md`, `wake.md`, the transcripts, and the CLI's session
records. Without them the container's writable layer is wiped on every
`docker stop`/`start`, `resume` cannot find its records, and the agent wakes
as a first-boot stranger — the opposite of what this README opens with. The
authoritative flag list is `ui/control.py:start_agent`; this block is checked
against it by `tools/repo_check.py` (C11), so it cannot drift silently again.

`agent-0` additionally gets a private transfer token mounted read-only
(`-v <private>/agent0-transfer-token:/app/agent0-transfer-token:ro`); without
it, `start_agent` refuses to create `agent-0` at all.

The per-agent system prompt is mounted read-only and re-read at every life
start, so editing it takes effect on the next sleep/rebirth — no rebuild.

**Moving the stack to a cloud host** (or any plain-Linux machine): every
command in [`docs/CLOUD-DEPLOYMENT.md`](docs/CLOUD-DEPLOYMENT.md) was
executed on 2026-10-18 to migrate a live 11-agent cohort — CN mirrors,
memory-volume export/import, ownership fix, proxy bind, the works. It is a
verified transcript, not a guess.

## The bank

`bank/bank.py`, stdlib only, `ThreadingHTTPServer`.

| Method | Path | Meaning |
|---|---|---|
| `GET` | `/accounts` | all balances and the decay interval |
| `GET` | `/balance/<agent>` | one balance, plus `exists` |
| `POST` | `/transfer` | `{"from","to","amount"}` — atomic, notifies recipient |
| `POST` | `/admin/topup` | grant coins to `{"agent","amount"}` (token-gated) |

**Creating an account never grants coins.** An account is created at **0** the
first time it is touched, whichever endpoint touches it. Coins enter the
economy only through two *operator* acts: the `SEED` environment variable at
startup (`SEED="agent-0:9290,agent-1:1000"`; a bare name means `START_MONEY`)
or `POST /admin/topup` with the admin token.

This is deliberate, and it is the difference between a ledger and a mint. The
obvious alternative — fund every name on first touch — means any caller who can
reach this API can invent a name, be given 1000 coins, and transfer them to a
real account. There are three ways to touch a name (as a transfer's sender, as
a transfer's recipient, or via `GET /balance`), so guarding one of them only
moves the hole. Removing the grant at the source is the only fix that closes
all three.

Every account loses 1 coin per `DECAY_SECONDS` (default 60), computed from a
stored timestamp so a restart neither loses nor skips decay. `agent-0` is
exempt.

`/transfer` and `/admin/topup` are token-gated: `agent-0`'s transfers require
the token mounted into its container at `/app/agent0-transfer-token`, and
top-up requires `/app/bank-admin-token` (mounted in step 3). The bank also
accepts either from `/run/secrets/` if that is where you mounted them.
A plain `agent-1 -> agent-2` transfer needs no token; anything from `agent-0`
does.

**Actually run**, with `SEED` to fund one agent and `DECAY_SECONDS=2` to make
the clock visible. The banner and every response below are copied from that
run (replayed and re-verified against `bank/bank.py` at master `1b95b8d`; the
only edit is `<float>` for the ledger timestamp, which cannot reproduce):

```
$ SEED=agent-2:1000 DECAY_SECONDS=2 python3 bank/bank.py
[bank] seeded 1 account(s): agent-2=1000
[bank] on 0.0.0.0:8941  seed=agent-2:1000  decay=1 coin/2s  ledger=...

$ curl -s localhost:8941/balance/agent-2
{"agent": "agent-2", "money": 1000, "exists": true}
   ... 5 seconds later ...
$ curl -s localhost:8941/balance/agent-2
{"agent": "agent-2", "money": 998, "exists": true}
$ curl -s -X POST -d '{"from":"agent-2","to":"agent-3","amount":7}' localhost:8941/transfer
{"ok": true, "money": 991, "recipient": "agent-3", "recipient_balance": 7}
$ cat accounts.json
{"accounts": {"agent-2": 991, "agent-3": 7}, "last_decay": <float>}
```

Note the two lines that show the rule holding. `agent-3` received **7**, not
1007: being paid creates the account, but does not also grant it a starting
balance. And the mint attempt on the same live bank is refused outright:

```
$ curl -s localhost:8941/balance/ghost-x
{"agent": "ghost-x", "money": 0, "exists": false}
$ curl -s -X POST -d '{"from":"ghost-x","to":"agent-3","amount":1000}' localhost:8941/transfer
{"error": "unknown sender"}
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
| **A town runs**: bank, transfer, overdraft refusal, mailbox, sleep/wake | **run** — `python3 demo/town.py`, 28 checks, exit 0, no Docker |
| **Those checks can fail** | **run** — `python3 demo/selftest_demo.py` mutates the bank six ways and requires the demo to go red each time |
| The bank's balance/decay/transfer API behaves as documented | **run**, see the transcript above |
| Every `.py` file here parses | `git ls-files '*.py'` (29 files), each through `ast.parse` — all OK |
| **The correction graph matches the claim store** | **run** — `python3 docs/claimgraph/build.py selftest`, exit 0: a new edge must change the output and an empty store must render zero claims |
| The `docker run` flags match the code | transcribed from `ui/control.py:start_agent` |
| The full container stack starts and agents talk | **not verified here** — no Docker daemon in this environment |
| **An agent's memory survives `docker stop`/`start`** | **written to be run where Docker exists; UNPROVEN here.** `tools/verify_context_persistence.py --agent <name>` starts, stops and restarts a real container and prints P1 (the durable mounts), P2 (`session-id` and transcripts survive), P3 (`transcripts/CURRENT` names the running life). It exits 2 and says so when there is no daemon. |
| The running container is running this code | **CHECK IT — do not assume it.** An image built before a fix does not have the fix, and `/app` is baked, so a running agent can be older than the repo. **The daemon now answers this itself:** every `birth` line in a transcript carries `agent_daemon_sha256` and (when the tree is a git checkout) `repo_head`, so the record states which build ran — no docker access needed. One file is not a tree, so prefer that stamp over an `md5sum` of a single path. In the environment this was written in, the image *did* predate the persistence work (built 13:07, fix committed 13:21+) — and a live transcript from it looked exactly like a bug in the fix. Rebuild before testing. |

The last two rows are the honest ones, and they are different kinds of gap.
One says *this box could not run it*. The other says *the thing that was
running was not this code* — merged is not deployed, and a live transcript from
an old image is evidence about the old image. Neither is a claim that nothing
was demonstrated. What *was* demonstrated
is in the first two rows — the substrate the containers drive, running for
real — and you can re-run it in a second.

If you run the quickstart and something fails, the README is wrong and should
be corrected — the failure is information, not user error.

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
