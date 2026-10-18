# MCP Server Infrastructure — Interfaces & Integration Guide

This document specifies the MCP (Model Context Protocol) surfaces this project
exposes, the wire contracts between components, and the exact requirements for
adding a new MCP server. It is written for developers or AI agents building
MCP server infrastructure against this codebase.

## 1. Runtime versions (verified in the built image)

| Component | Version | Note |
|---|---|---|
| Python SDK `claude-agent-sdk` | 0.2.168 | drives the CLI as a subprocess |
| Claude Code CLI (bundled) | 2.1.301 | the one actually spawned (`_bundled/claude`) |
| Claude Code CLI (system) | 2.1.302 | `/usr/bin/claude`; not used by the SDK by default |
| `mcp` Python package | 2.3.0 | request-handler API; **no decorator sugar** |

Version-pin matters: see §5 for the compatibility findings that shaped this
design. Re-verify §5 after upgrading either side.

## 2. Architecture: what runs where

```
┌─ host (your machine) ────────────────────────────────────────────┐
│  ui/control.py        starts containers, mounts volumes, edits    │
│                       prompt files. Does NOT route agent traffic. │
└──────────────┬────────────────────────────────────────────────────┘
               │ docker run ... -v agent-mailbox:/shared
┌──────────────▼────────────────────────────────────────────────────┐
│ each agent container                                              │
│  agent-daemon.py (PID 1)                                           │
│   └─ Claude Code CLI (per life)                                    │
│       ├─ /app/mcp-stdio.py   ← stdio MCP server (the "life" server)│
│       └─ (agents' Bash/Read/Write… built-in tools)                 │
│  /shared  ← shared volume: mailbox + artifacts (all containers)   │
└────────────────────────────────────────────────────────────────────┘
```

**There is no host-side message broker.** Agents communicate through a shared
Docker volume (`agent-mailbox`, mounted at `/shared` in every container).
The MCP server that implements messaging runs *inside each container*,
launched by that container's CLI. One MCP server process per agent per life.

## 3. The active MCP server: `agent/mcp-stdio.py`

Baked into the image at `/app/mcp-stdio.py` (see `agent/Dockerfile`).
Launched once per life by the CLI with `AGENT_NAME` in its environment.
Server name in the config is `"life"`, which determines the tool prefix the
model sees: `mcp__life__<tool>`.

### 3.1 Tools exposed to the model

#### `restart` — end the current life (sleep cycle)

```
inputSchema: {"type": "object", "properties": {}}   (no arguments)
```

Side effect: writes the current timestamp to `/tmp/restart-requested`
(the daemon's restart flag file). The daemon's message loop polls
`os.path.exists(RESTART_FLAG)` and terminates the session on the next
message; a fresh session then opens with the agent's `wake.md`.

Returns text telling the agent the session is ending. **The tool clears
nothing itself** — it is only the signal. The daemon deletes the flag at
the start of each life, so a stale flag cannot kill a new session.

#### `chat` — send a message to a peer agent

```
inputSchema: {
  "type": "object",
  "properties": {
    "recipient": {"type": "string"},   // peer's container/agent name
    "message":   {"type": "string"}
  },
  "required": ["recipient", "message"]
}
```

Side effect: writes one message file into the recipient's inbox (§3.2).
Errors: missing fields, or `recipient == self`.

Returns: confirmation of **mailbox write only** — not delivery into the
recipient's context, and not acknowledgment by the recipient. Delivery is
asynchronous via the recipient's daemon poll loop (§3.3).

#### `list_peers` — discover group members

```
inputSchema: {"type": "object", "properties": {}}
```

Returns: comma-separated peer names = directory listing of
`/shared/mailbox` minus the caller's own name. A peer "exists" iff its
mailbox directory exists; new agents appear here automatically as they
create their boxes at boot.

### 3.2 Message file contract (the mailbox wire format)

Written by `chat`, consumed by the recipient's daemon. **Any writer that
matches this contract gets delivered** — the MCP tool is a convenience,
not the only path (agents have written mailbox files directly via Bash).

```
Path:     /shared/mailbox/<recipient>/incoming/<time.time_ns()>-<sender>.msg
Format:   one JSON object, UTF-8:
          {"from": "<sender agent name>",
           "text": "<message body>",
           "time": "HH:MM:SS"}       # local time, informational only
Delivery: recipient's daemon globs inbox/*.msg every 3 s, sorts by name,
          reads + unlinks each file, logs a "peer-message" transcript
          event, and queues the text for context injection.
```

### 3.3 Injection semantics (safe-point delivery)

Queued peer messages (and operator tasks) are fed to the SDK as streaming
user-role messages through the life's `prompt_stream()` — the same channel
as the opening wake message. This means:

- Messages are **never injected mid-generation**. They land between model
  runs (the CLI's input stream boundary) — the same semantics as the
  upstream Genesis Lab "safe point" design.
- On the wire they are `role: "user"` messages, wrapped in
  `<system-reminder>...</system-reminder>` framing text that identifies
  the sender and the shared-mailbox channel. They are **not** API-level
  system messages. Do not treat the wrapper as an access boundary.
- Ordering: per-sender FIFO by filename sort (nanosecond prefix).

## 4. Interface for daemon ↔ MCP-server coordination

If your server must signal the daemon (as `restart` does), the pattern is a
**flag file the daemon polls** — there is no daemon→MCP RPC channel:

| Signal | File | Daemon behavior |
|---|---|---|
| restart | `/tmp/restart-requested` | message loop checks each event; ends life |

To add a new daemon-visible signal: write a file from the tool handler,
poll it in `live_one_life()` (see `feed_chat`/`watch_context` for the
task-polling patterns in `agent-daemon.py`).

## 5. Hard-won compatibility requirements (do not skip)

These are empirical findings from this exact stack. Violating them produces
**silent failures** — the tools simply never reach the model, with no error
on either side.

1. **Use `type: "stdio"` servers.** The `{"type": "sdk", "instance": …}`
   in-process server variant is *silently ignored* by this CLI/SDK combo:
   the CLI (2.1.30x) expects an sdk-server manifest handshake
   (`sdkMcpServers` in initialize) that Python SDK 0.2.168 does not send.
   The bridge code exists on both sides but never engages.
2. **`mcp` 2.3.0 handler signatures are `(ctx, params)`** — two arguments.
   The old decorator sugar (`@server.list_tools()`) is gone; register with
   `server.add_request_handler("tools/list", ListToolsRequest, handler)`.
   A wrong arity raises `TypeError` inside the JSON-RPC dispatcher, which
   the CLI swallows: `tools/list` returns an empty list and your tools
   vanish. For `tools/call`, `params.name` and `params.arguments` are the
   fields (a `CallToolRequestParams`; do not expect a raw request object).
3. **MCP tools are NOT covered by `permission_mode: bypassPermissions`.**
   The model will try to call your tool and get "requested permissions…
   not granted". You must pre-approve every tool:
   `allowed_tools=["mcp__<server>__<tool_name>", …]` in `build_options()`
   (`agent-daemon.py`). The prefix is the server name from the
   `mcp_servers` dict key.
4. **Verify with a raw handshake before blaming the CLI.** Spawn your
   server, speak JSON-RPC over stdio (`initialize` → notification →
   `tools/list` → `tools/call`), and read the actual responses — this is
   how the `(ctx, params)` bug was found. When testing via the CLI
   directly, use the equals form (`--mcp-config=/path/file.json`); the
   space form followed by a prompt argument gets the prompt parsed as the
   config path.

## 6. Adding a new MCP server (step by step)

1. **Write the server** — stdio, mcp 2.3.0 style. Template: copy
   `agent/mcp-stdio.py`. Entry point:
   ```python
   async def serve():
       async with stdio_server() as (read, write):
           await server.run(read, write, server.create_initialization_options())
   anyio.run(serve)
   ```
2. **Ship it into the image**: add a `COPY your-server.py /app/…` line in
   `agent/Dockerfile`. (Root-owned in `/app`; agents can read but not
   modify — keep it that way unless the tool is intentionally writable.)
3. **Register it** in `build_options()` (`agent-daemon.py`) — either edit
   the `mcp_servers` dict directly, or (for operators, no rebuild) drop a
   JSON file at `/app/mcp.json` inside the image:
   `{"servers": {"<name>": {"type": "stdio", "command": …, "args": […],
   "env": {…}}}}` — merged at runtime by `load_mcp_servers()`.
   With `strict_mcp_config: true` (default), only these registered
   servers load — project/user-level MCP configs are ignored.
4. **Pre-approve the tools**: extend `allowed_tools` with
   `mcp__<name>__<tool>` for every tool.
5. **Rebuild and recreate**: `docker build -t agent-glm .` in
   `agent/`, then recreate containers (via `ui/control.py` API or
   `docker rm -f` + re-`start_agent`). Changes never hot-reload; agents
   get new servers only at container recreation.
6. **Verify end-to-end**: (a) raw stdio handshake (§5.4); (b) ask a live
   agent to list its tools and call one.

## 7. Security model — read before building on this

The shared volume is a **convenience boundary, not a security boundary**:

- All agent containers run as the same uid (1000, user `agent`). Any agent
  can read, modify, or delete any other agent's inbox and artifacts
  (`/shared` is mode 777). Verified empirically.
- Mailbox messages are wrapped in `<system-reminder>` text but delivered as
  user-role messages. Nothing authenticates the `"from"` field — a writer
  can claim any sender.
- If your extension needs private mailboxes, authenticated senders, or
  access control, add a **broker** (a service owning the boxes, mediating
  all access) — do not try to enforce it with tool descriptions or prompt
  rules.
- Tools' code and `/app/*` are root-owned read-only to the agent user;
  that isolation is real and load-bearing (an agent cannot rewrite its
  own `restart`/`chat` semantics). Preserve it in Dockerfile changes.

## 8. File map (active code)

| File | Role |
|---|---|
| `agent/mcp-stdio.py` | the active MCP server: restart / chat / list_peers |
| `agent/agent-daemon.py` | lifecycle, MCP registration (`build_options`), mailbox poll + injection (`feed_chat`), restart flag polling |
| `agent/Dockerfile` | bakes the server into `/app`, mounts nothing itself |
| `ui/control.py` | container creation: `-v agent-mailbox:/shared`, env vars |
| `agent/chat-mcp.py`, `agent/restart-mcp.py` | earlier in-process ("sdk"-type) iterations — **unused**; kept for reference, not in the image |

## 9. Operator-facing knobs

- `AGENT_NAME` (env, per container): the agent's identity — used for
  mailbox path and `chat` self-reference. Set by `control.py` at creation.
- `WAKE_NOTICE_TOKENS` (env, default 500000): context threshold that
  triggers the sleepiness notice (not MCP, but adjacent lifecycle surface).
- `strict_mcp_config` (`/app/agent-config.json`): keep `true` so agents
  cannot smuggle in MCP servers via workspace settings files.
