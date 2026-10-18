"""Long-running Claude agent daemon with a sleep/wake (restart) cycle.

Mechanism:
  - Each "life" is one SDK session with the operator's system prompt.
  - At life start, the daemon feeds the agent a harness-composed opening
    message: the agent's own wake-up prompt (/workspace/self/wake.md, a
    file it wrote to itself before its last sleep) framed as such. This
    is NOT a user prompt — the user never talks to this agent directly.
  - The daemon watches context usage (get_context_usage). At WAKE_NOTICE
    tokens (default 500k) it injects a notice telling the agent to wrap
    up, write its diary + next wake-up prompt, then call the `restart`
    skill. The `restart` skill is intercepted by a PreToolUse-style
    check in the message loop and ends the life: a fresh session starts
    with the same system prompt and the new wake-up prompt.
  - Tasks arrive via /workspace/inbox/*.task (submitted with the
    agent-task helper) and are fed into the current life's conversation.
  - Transcripts per life under /workspace/transcripts/, replies under
    /workspace/replies/.

Everything the agent writes stays inside the container's /workspace.
This script and /app/agent-config.json are root-owned; the agent cannot
modify them.
"""

import asyncio
import glob
import json
import os
import sys
import time

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    ResultMessage,
    SystemMessage,
    TextBlock,
    UserMessage,
    query,
)

# Life-cycle MCP tools (restart / chat / list_peers) are provided by the
# stdio server /app/mcp-stdio.py (see build_options).

AGENT_NAME = os.environ.get("AGENT_NAME", "glm-agent")
AGENT_MODEL = os.environ.get("AGENT_MODEL", "glm-5.3")
SHARED = "/shared"
MAILBOX = os.path.join(SHARED, "mailbox")
MY_INBOX = os.path.join(MAILBOX, AGENT_NAME, "incoming")

WORKSPACE = "/workspace"
HUMAN_INBOX = os.path.join(WORKSPACE, "human", "inbox")
HUMAN_OUTBOX = os.path.join(WORKSPACE, "human", "outbox")
SELF_DIR = os.path.join(WORKSPACE, "self")
WAKE_FILE = os.path.join(SELF_DIR, "wake.md")
DIARY_FILE = os.path.join(SELF_DIR, "diary.md")
SESSION_FILE = os.path.join(SELF_DIR, "session-id")
INBOX = os.path.join(WORKSPACE, "inbox")
REPLIES = os.path.join(WORKSPACE, "replies")
TRANSCRIPTS = os.path.join(WORKSPACE, "transcripts")
MCP_CONFIG = "/app/mcp.json"

WAKE_NOTICE_TOKENS = int(os.environ.get("WAKE_NOTICE_TOKENS", "500000"))
RESTART_FLAG = "/tmp/restart-requested"

PROXY_URL = os.environ.get("PROXY_URL", "http://host.docker.internal:8787")

ENV = {
    "ANTHROPIC_BASE_URL": PROXY_URL,
    "ANTHROPIC_AUTH_TOKEN": "local-proxy",
    "ANTHROPIC_MODEL": AGENT_MODEL,
    # glm-5.3 has a 1M context window; the CLI would assume 200k without this.
    "CLAUDE_CODE_MAX_CONTEXT_TOKENS": "1000000",
    "HOME": "/home/agent",
    "DISABLE_TELEMETRY": "1",
    "DISABLE_ERROR_REPORTING": "1",
}


# --------------------------------------------------------------------------
# Config / prompt assembly
# --------------------------------------------------------------------------

def load_agent_config() -> dict:
    path = "/app/agent-config.json"
    if not os.path.exists(path):
        return {}
    with open(path) as f:
        return json.load(f)


def load_mcp_servers():
    if not os.path.exists(MCP_CONFIG):
        return {}
    with open(MCP_CONFIG) as f:
        data = json.load(f)
    return data.get("servers", data)


def read_resumable_session() -> str | None:
    """Session id to resume, or None for a fresh session.

    Resume when the container restarted mid-session (crash, docker stop,
    Docker Desktop outage): the CLI's session records live under the
    agent user's home, which survives container stop/start, so resuming
    restores the FULL context window instead of waking amnesiac. The id
    is written by the message loop when the init frame arrives.
    """
    try:
        with open(SESSION_FILE, encoding="utf-8") as f:
            sid = f.read().strip()
        return sid or None
    except OSError:
        return None


def build_options(cfg: dict):
    # Life-cycle tools (restart/chat/list_peers) come from a stdio MCP
    # server this daemon launches per life. The CLI's sdk-type in-process
    # servers are not driven by this SDK/CLI combo (manifest protocol
    # mismatch), so stdio is the reliable path. The restart signal is a
    # flag file the message loop polls; chat writes the shared mailbox.
    opts = dict(
        # Env var (per-container, set by the control UI) wins over config.
        model=AGENT_MODEL or cfg.get("model") or "glm-5.3",
        cwd=WORKSPACE,
        permission_mode="bypassPermissions",
        env={**ENV, **cfg.get("extra_env", {})},
        mcp_servers={
            "life": {
                "type": "stdio",
                "command": "python3",
                "args": ["/app/mcp-stdio.py"],
                "env": {"AGENT_NAME": AGENT_NAME},
            },
            **{k: v for k, v in load_mcp_servers().items()},
        },
        # bypassPermissions does not cover MCP tools; pre-approve ours.
        # talk_to_stanley is only registered when AGENT_NAME == "agent-0";
        # pre-approving it everywhere is harmless (unknown allowed tools
        # are ignored by the CLI).
        allowed_tools=["mcp__life__restart", "mcp__life__chat",
                        "mcp__life__list_peers",
                        "mcp__life__check_money", "mcp__life__send_money",
                        "mcp__life__talk_to_stanley"],
        setting_sources=[],
        strict_mcp_config=cfg.get("strict_mcp_config", True),
    )
    # Container restart resume: continue the exact prior session's context
    # when one was left behind (see read_resumable_session). Not for
    # life-restarts — the restart tool clears the session file first.
    resume_sid = read_resumable_session()
    if resume_sid:
        opts["resume"] = resume_sid
        print(f"[daemon] resuming session {resume_sid}", flush=True)
    if "system_prompt" in cfg:
        opts["system_prompt"] = cfg["system_prompt"]
    elif cfg.get("system_prompt_file") and os.path.exists(cfg["system_prompt_file"]):
        opts["system_prompt"] = {
            "type": "file", "path": cfg["system_prompt_file"],
        }
    elif cfg.get("system_prompt_append"):
        opts["system_prompt"] = {
            "type": "preset", "preset": "claude_code",
            "append": cfg["system_prompt_append"],
        }
    if cfg.get("tools_preset"):
        opts["tools"] = {"type": "preset", "preset": cfg["tools_preset"]}
    if cfg.get("allowed_tools"):
        opts["allowed_tools"] = cfg["allowed_tools"]
    if cfg.get("disallowed_tools"):
        opts["disallowed_tools"] = cfg["disallowed_tools"]
    if cfg.get("skills"):
        opts["skills"] = cfg["skills"]

    # Genesis-lab pattern (bridge.handle_hook): messages are consumed from
    # their sources by the daemon's poll loops (feed_chat / feed_tasks /
    # feed_human), which log them and push the formatted text onto a
    # shared queue. The PostToolUse hook then drains THAT queue at every
    # tool boundary — including after the agent's own `sleep 300` Bash
    # polls — so mail queued during a long tool call is seen the moment
    # the call returns, instead of waiting for the whole turn to end.
    # (v1 of this hook read the inbox files directly and raced the poll
    # loops; whoever won, delivery lost — fixed by single-consumer.)
    from claude_agent_sdk.types import HookMatcher
    opts["hooks"] = {
        "PostToolUse": [HookMatcher(hooks=[deliver_pending_hook])],
    }

    return ClaudeAgentOptions(**opts)


# Per-life delivery queue shared between the poll loops (producers) and
# the PostToolUse hook (consumer). Replaced at each life start.
pending_delivery: list[str] = []


def drain_pending_delivery() -> str | None:
    """Take everything queued since the last delivery, as one batch."""
    if not pending_delivery:
        return None
    texts = pending_delivery[:]
    pending_delivery.clear()
    return "\n\n".join(texts)


async def deliver_pending_hook(input, tool_use_id, context) -> dict:
    """PostToolUse hook body: drain the daemon's delivery queue into the
    live context. Async per the SDK's HookCallback contract
    (input, tool_use_id, context) -> awaitable."""
    combined = drain_pending_delivery()
    if not combined:
        return {}
    return {"hookSpecificOutput": {
        "hookEventName": "PostToolUse",
        "additionalContext":
            "<system-reminder>\n" + combined + "\n</system-reminder>",
    }}


def read_wake_prompt() -> str:
    """The agent's note to itself from its last sleep, or a first-boot text."""
    if os.path.exists(WAKE_FILE):
        with open(WAKE_FILE) as f:
            content = f.read().strip()
        if content:
            return content
    return (
        "This is your first boot. There is no wake-up prompt yet. "
        "You have no goals unless you set them yourself: feel free to "
        "write /workspace/self/wake.md with instructions for your next "
        "boot, and /workspace/self/diary.md to keep your ongoing notes."
    )


def opening_message(wake_prompt: str) -> str:
    """The harness-composed first message of every life. NOT from a user."""
    return (
        "<system-reminder>\n"
        "This is the start of a new session. Your previous session's "
        "context has been cleared (you 'slept'). Below is your wake-up "
        "prompt: a note you wrote to yourself in /workspace/self/wake.md "
        "before you slept. Your system prompt, your files in "
        "/workspace (including your diary at /workspace/self/diary.md), "
        "are all yours to continue from. Treat the wake-up prompt as your "
        "own plan for this session, not as a user's message.\n"
        "</system-reminder>\n\n"
        "Your wake-up prompt to yourself:\n\n"
        f"{wake_prompt}"
    )


# --------------------------------------------------------------------------
# Transcript logging
# --------------------------------------------------------------------------

class LifeLog:
    """JSONL transcript for one life."""

    def __init__(self, life_id: str):
        os.makedirs(TRANSCRIPTS, exist_ok=True)
        self.path = os.path.join(TRANSCRIPTS, f"life-{life_id}.jsonl")

    def log(self, kind: str, **fields):
        entry = {"time": time.strftime("%H:%M:%S"), "kind": kind, **fields}
        with open(self.path, "a") as f:
            f.write(json.dumps(entry, default=str) + "\n")


# --------------------------------------------------------------------------
# The sleep / wake life-cycle
# --------------------------------------------------------------------------

class Death(Exception):
    """Raised inside the message loop when the agent calls restart."""


async def live_one_life(life_id: str, cfg: dict, inbox_next: asyncio.Queue):
    """Run one complete life: from wake to restart. Returns wake data for
    the next life (None when the agent never slept — e.g. crash)."""

    log = LifeLog(life_id)
    log.log("birth", model=cfg.get("model", AGENT_MODEL),
            wake_file=os.path.exists(WAKE_FILE))

    wake_prompt = read_wake_prompt()
    try:
        os.remove(RESTART_FLAG)  # fresh life: no stale restart signal
    except OSError:
        pass
    def restart_requested_flag():
        return os.path.exists(RESTART_FLAG)

    options = build_options(cfg)
    resumed = getattr(options, "resume", None) is not None

    restart_requested = False
    notice_sent = False

    async with ClaudeSDKClient(options=options) as client:
        # Feed the opening message as a streaming prompt. When resuming a
        # crashed session the conversation already exists — re-sending the
        # wake framing would corrupt it, so the stream starts empty and the
        # CLI replays history on its own.
        async def prompt_stream():
            if not resumed:
                yield {"type": "user",
                       "message": {"role": "user", "content": opening_message(wake_prompt)}}
            while True:
                item = await inbox_next.get()
                if item is None:
                    break
                # "__kick__" markers mean "mail arrived". The hook delivers
                # it mid-run at tool boundaries. Only if a marker survives
                # until a RUN boundary (this generator resuming = previous
                # run done, SDK accepting the next user message) do we send
                # real text — batching whatever is still queued. This is the
                # genesis-lab wake_if_pending path.
                if item == "__kick__":
                    combined = drain_pending_delivery()
                    if combined:
                        yield {"type": "user",
                               "message": {"role": "user", "content":
                                           "<system-reminder>\n" + combined +
                                           "\n</system-reminder>"}}
                    continue
                yield {"type": "user",
                        "message": {"role": "user", "content": item}}

        async def watch_context():
            """Poll usage; at WAKE_NOTICE_TOKENS inject the sleepiness notice."""
            nonlocal notice_sent
            while True:
                await asyncio.sleep(30)
                try:
                    usage = await client.get_context_usage()
                    total = usage.get("totalTokens", 0)
                except Exception:
                    continue
                log.log("context", total_tokens=total)
                if not notice_sent and total >= WAKE_NOTICE_TOKENS:
                    notice_sent = True
                    log.log("wake-notice", total_tokens=total)
                    await client.query(
                        "<system-reminder>\n"
                        f"Your context usage has reached {total} tokens "
                        f"(threshold: {WAKE_NOTICE_TOKENS}). You are getting "
                        "close to your limit. From now on: finish your "
                        "current work without starting anything new or "
                        "context-heavy. Update your diary "
                        "(/workspace/self/diary.md) and write your "
                        "wake-up prompt (/workspace/self/wake.md) for your "
                        "next session. When everything is settled, call the "
                        "restart skill to sleep.\n"
                        "</system-reminder>"
                    )

        async def feed_tasks():
            """Move inbox task files into the delivery queue."""
            while True:
                await asyncio.sleep(2)
                for path in sorted(glob.glob(os.path.join(INBOX, "*.task"))):
                    try:
                        with open(path) as f:
                            task = f.read().strip()
                        os.remove(path)
                    except OSError:
                        continue
                    if task:
                        log.log("task", file=os.path.basename(path), text=task)
                        pending_delivery.append(
                            "New task from the operator: \n\n" + task)
                        await inbox_next.put("__kick__")

        async def feed_chat():
            """Poll the shared mailbox for peer messages; format and queue
            them for delivery (hook at tool boundaries, or a new run when
            the current one ends)."""
            while True:
                await asyncio.sleep(3)
                try:
                    files = sorted(glob.glob(os.path.join(MY_INBOX, "*.msg")))
                except Exception:
                    continue
                for path in files:
                    try:
                        with open(path) as f:
                            msg = json.load(f)
                        os.remove(path)
                    except (OSError, json.JSONDecodeError):
                        continue
                    sender = msg.get("from", "unknown")
                    text = msg.get("text", "").strip()
                    if not text:
                        continue
                    log.log("peer-message", **{"from": sender}, text=text)
                    pending_delivery.append(
                        f"A message arrived from your peer '{sender}' "
                        "(another agent in your group, delivered via the "
                        "shared mailbox): \n\n" + text)
                    await inbox_next.put("__kick__")

        async def feed_human():
            """Only the delegate consumes the private Stanley inbox."""
            while True:
                await asyncio.sleep(2)
                for path in sorted(glob.glob(os.path.join(HUMAN_INBOX, "*.msg"))):
                    try:
                        with open(path, encoding="utf-8") as f:
                            msg = json.load(f)
                        text = msg.get("text", "").strip()
                        if text:
                            pending_delivery.append(
                                "Stanley has sent you a message through your "
                                "private human channel:\n\n" + text)
                            log.log("human-message", text=text, id=msg.get("id"))
                            await inbox_next.put("__kick__")
                        os.remove(path)
                    except (OSError, json.JSONDecodeError):
                        continue

        feed = asyncio.create_task(feed_tasks())
        watcher = asyncio.create_task(watch_context())
        chat = asyncio.create_task(feed_chat())
        human = asyncio.create_task(feed_human()) if AGENT_NAME == "agent-0" else None

        try:
            # Start the life: feed the streaming prompt (opening message +
            # tasks), and consume the agent's messages.
            send_task = asyncio.create_task(client.query(prompt_stream()))

            async for message in client.receive_response():
                if restart_requested_flag():  # agent called the restart tool
                    log.log("restart-called")
                    raise Death()
                if isinstance(message, AssistantMessage):
                    for block in message.content:
                        if isinstance(block, TextBlock):
                            log.log("assistant", text=block.text)
                            print(block.text, flush=True)
                        else:
                            log.log("assistant-block",
                                    block_class=type(block).__name__,
                                    **({"name": block.name, "input": block.input}
                                       if hasattr(block, "name") else {}))
                elif isinstance(message, UserMessage):
                    for block in message.content:
                        if not isinstance(block, TextBlock):
                            log.log("tool-result",
                                    content=getattr(block, "content", str(block)))
                elif isinstance(message, SystemMessage):
                    log.log("system", subtype=getattr(message, "subtype", ""))
                    if getattr(message, "subtype", "") == "init":
                        sid = (getattr(message, "data", None) or {}).get("session_id")
                        if sid:
                            try:
                                with open(SESSION_FILE, "w", encoding="utf-8") as f:
                                    f.write(sid)
                            except OSError:
                                pass
                elif isinstance(message, ResultMessage):
                    log.log("result", subtype=message.subtype,
                            turns=message.num_turns, cost=message.total_cost_usd)
        except Death:
            restart_requested = True
            log.log("death", reason="restart-skill")
            # Deliberate sleep ≠ container restart: next life starts fresh
            # (diary/wake carry the memory), so the session id must not be
            # resumed. Delete it to distinguish from a crash-restart.
            try:
                os.remove(SESSION_FILE)
            except OSError:
                pass
        finally:
            tasks = [feed, watcher, chat] + ([human] if human else [])
            for t in tasks:
                t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    return restart_requested


def start_viewer():
    """Run the conversation-history web UI in a daemon thread."""
    import threading
    import viewer as viewer_mod

    def serve():
        viewer_mod.serve_forever()

    threading.Thread(target=serve, daemon=True).start()


def ensure_mailbox():
    """Create this agent's mailbox dirs in the shared volume. The volume's
    mailbox dir is pre-created mode-777 (first boot: by an earlier agent or
    the image); chmod is best-effort — an EPERM on a fresh root-owned
    volume is fine as long as we can create our own subdirectory."""
    try:
        os.makedirs(MY_INBOX, exist_ok=True)
        return
    except OSError:
        pass
    # Mailbox root missing or closed: try to create/open it.
    try:
        os.makedirs(MAILBOX, exist_ok=True)
        os.chmod(MAILBOX, 0o777)
        os.makedirs(MY_INBOX, exist_ok=True)
    except OSError as e:
        print(f"[daemon] mailbox unavailable: {e}", flush=True)


async def main_loop():
    for d in (INBOX, REPLIES, TRANSCRIPTS, SELF_DIR):
        os.makedirs(d, exist_ok=True)
    if AGENT_NAME == "agent-0":
        for d in (HUMAN_INBOX, HUMAN_OUTBOX):
            os.makedirs(d, exist_ok=True)
    ensure_mailbox()

    start_viewer()
    cfg = load_agent_config()
    print(f"[daemon] up. wake-notice at {WAKE_NOTICE_TOKENS} tokens. "
          f"tasks: docker exec glm-agent agent-task \"...\"", flush=True)
    print(f"[daemon] conversation UI on http://localhost:8080 "
          f"(port 8080 published)", flush=True)

    life_no = 0
    while True:
        life_no += 1
        life_id = f"{int(time.time())}-{life_no}"
        print(f"[daemon] life {life_id} starting", flush=True)
        inbox_next = asyncio.Queue()
        try:
            slept = await live_one_life(life_id, cfg, inbox_next)
        except Exception as exc:
            print(f"[daemon] life {life_id} crashed: {exc}; reborn in 10s",
                  flush=True)
            await asyncio.sleep(10)
            continue
        if slept:
            print(f"[daemon] life {life_id} slept. waking fresh", flush=True)
        else:
            print(f"[daemon] life {life_id} ended without sleep; "
                  f"restarting in 10s", flush=True)
            await asyncio.sleep(10)


if __name__ == "__main__":
    if len(sys.argv) > 1:  # one-shot mode, for testing
        os.makedirs(TRANSCRIPTS, exist_ok=True)
        cfg = load_agent_config()

        async def one_shot():
            q: asyncio.Queue = asyncio.Queue()
            await q.put(" ".join(sys.argv[1:]))
            await q.put(None)
            await live_one_life("oneshot", cfg, q)

        asyncio.run(one_shot())
    else:
        asyncio.run(main_loop())
