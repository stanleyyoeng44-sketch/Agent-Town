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
import re
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
# A file this large is not a chat message. The salvage path in _parse_message
# reads a non-JSON body into memory and queues it to the model, so it must have
# a ceiling; a stray dump in an inbox should be reported, not delivered.
MAX_SALVAGE_BYTES = 256 * 1024

WORKSPACE = "/workspace"
HUMAN_INBOX = os.path.join(WORKSPACE, "human", "inbox")
HUMAN_OUTBOX = os.path.join(WORKSPACE, "human", "outbox")
SELF_DIR = os.path.join(WORKSPACE, "self")
WAKE_FILE = os.path.join(SELF_DIR, "wake.md")
DIARY_FILE = os.path.join(SELF_DIR, "diary.md")
SESSION_FILE = os.path.join(SELF_DIR, "session-id")
# The note an agent wrote may not be at SELF_DIR. SELF_DIR is the path the
# daemon and every tool's text agrees on, but it is durable ONLY when the
# operator gave the container a named volume for it -- and for most of the
# first cohort it was not, so agents wrote to the shared artifacts tree
# instead and the note was never read. These are the two layouts that tree
# actually uses (measured on the 2026-10-18 cohort: seven agents at the root,
# one under self/). Suffix-free: the filename is appended by wake_candidates.
ARTIFACT_FALLBACK_DIRS = (
    os.path.join(SHARED, "artifacts", AGENT_NAME),
    os.path.join(SHARED, "artifacts", AGENT_NAME, "self"),
)
# A wake.md found outside SELF_DIR is used, but a copy there that DISAGREES
# with it is not silently ignored: see read_wake_prompt()'s return of why.
WAKE_SOURCE_FALLBACK = "fallback"
INBOX = os.path.join(WORKSPACE, "inbox")
REPLIES = os.path.join(WORKSPACE, "replies")
TRANSCRIPTS = os.path.join(WORKSPACE, "transcripts")
# Which life is CURRENT, for the operator UI. It cannot be inferred from the
# filenames: lives are named by timestamp, and after a container restart the
# newest file is the life that was interrupted, not the one running now. The
# daemon writes this at life start (and it is removed on a clean sleep).
CURRENT_LIFE_FILE = os.path.join(TRANSCRIPTS, "CURRENT")
MCP_CONFIG = "/app/mcp.json"

# Context-usage threshold that triggers the sleepiness notice (tokens).
WAKE_NOTICE_TOKENS = int(os.environ.get("WAKE_NOTICE_TOKENS", "500000"))

# The CLI keeps its session records (and therefore the ONE place a resumed
# context can come from) under ~/.claude. If that stays in the container's
# writable layer, `docker stop/start` wipes it along with /workspace, so
# read_resumable_session() can name a session whose records are gone -- the
# resume path is dead code at exactly the moment it exists for. Put it on a
# volume the operator can persist, and pass it to the SDK subprocess so the
# SDK resolves the same directory it writes to.
CONFIG_DIR = os.environ.get("CLAUDE_CONFIG_DIR", "/workspace/.claude")
RESTART_FLAG = "/tmp/restart-requested"

PROXY_URL = os.environ.get("PROXY_URL", "http://host.docker.internal:8787")

ENV = {
    "ANTHROPIC_BASE_URL": PROXY_URL,
    "ANTHROPIC_AUTH_TOKEN": "local-proxy",
    "ANTHROPIC_MODEL": AGENT_MODEL,
    # glm-5.3 has a 1M context window; the CLI would assume 200k without this.
    "CLAUDE_CODE_MAX_CONTEXT_TOKENS": "1000000",
    "HOME": "/home/agent",
    # Session records live here; must match the volume mounted by the UI.
    "CLAUDE_CONFIG_DIR": CONFIG_DIR,
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


def peers_present() -> int:
    """How many other agents have a mailbox directory. Cheap, no bank call."""
    try:
        return sum(1 for d in os.listdir(MAILBOX)
                   if os.path.isdir(os.path.join(MAILBOX, d))
                   and d != AGENT_NAME)
    except OSError:
        return 0


def log_idle_proof(log, why: str, pending: int = 0) -> None:
    """Write ONE transcript line proving this life is alive and waiting.

    Stanley's defect: a restarted agent "will not have any response unless
    someone messages it". The cause is not a crash -- prompt_stream() feeds the
    model the opening message and then blocks on an empty inbox, and a model
    with nothing to do writes nothing, so the transcript contains only `birth`
    and the UI has nothing to show. The process is fine; the evidence is
    missing.

    This is evidence, not behaviour: a transcript line, no model turn, no
    tokens, no new wire format, nothing the agent can observe. It is written at
    three points -- when the life starts, whenever the agent finishes a run and
    goes quiet, and when the context watcher wakes up -- so ANY of the three
    readers of the transcript (UI, viewer, a human with `cat`) sees a live
    agent without having to talk to it.
    """
    log.log("idle", why=why, peers=peers_present(), pending=pending)


def sdk_session_records() -> str:
    """Where the CLI stores a session's records for this cwd.

    The SDK writes under <CLAUDE_CONFIG_DIR>/projects/<sanitized cwd>/.
    A session id is NOT resumable unless the directory for it still exists
    -- a written id alone turns a restart into a resume attempt that fails
    after the opening message has been suppressed (see live_one_life).
    """
    projects = os.path.join(CONFIG_DIR, "projects")
    want = re.sub(r"[^A-Za-z0-9]", "-", WORKSPACE)
    try:
        for entry in os.listdir(projects):
            if entry == want:
                return os.path.join(projects, entry)
    except OSError:
        pass
    return os.path.join(projects, want)


def build_stamp() -> dict:
    """What this process is actually running, recorded so it can be asked.

    agent-9's point: the README told an operator to md5 ONE file and conclude
    the container "is running this code". One file is not a tree -- today's
    work touched agent/agent-daemon.py, ui/control.py and the on-disk layout,
    so a daemon that matches while the rest does not would pass that check.
    A process that cannot say which build it is has to be interrogated from
    outside, and outside access is exactly what this box does not have.

    Recorded at every birth: the sha256 of the code this daemon can see, so a
    transcript answers 'was the fix running?' without docker access.
    """
    out = {"agent_daemon_sha256": _sha256(__file__)}
    try:
        out["repo_head"] = _read_head()
    except Exception:
        out["repo_head"] = None
    return out


def _sha256(path: str):
    import hashlib
    try:
        with open(path, "rb") as f:
            return hashlib.sha256(f.read()).hexdigest()
    except OSError:
        return None


def _read_head():
    """The commit this checkout is on, if the source tree is a git repo.

    BOUNDED ON PURPOSE. The daemon lives at /app inside the container, so
    walking up from it reaches / and then stops -- but if /app were a bind
    mount, walking further would read the HOST's .git. The search stops at the
    first of: the repo root, the filesystem root, or /app itself. Reading a
    git config is a harmless thing to do badly; reading one from outside the
    container because a mount made it reachable is not.
    """
    here = os.path.dirname(os.path.abspath(__file__))
    stop = os.path.abspath("/app")
    for _ in range(4):
        if here == os.path.dirname(here):      # filesystem root
            break
        if os.path.abspath(here) == stop and here != os.path.dirname(
                os.path.abspath(__file__)):
            # never search above the baked app directory
            break
        head = os.path.join(here, ".git", "HEAD")
        if os.path.isfile(head):
            with open(head, encoding="utf-8") as f:
                ref = f.read().strip()
            if ref.startswith("ref: "):
                ref_path = os.path.join(here, ".git", ref[5:])
                # a ref that escapes the repo is not a ref we should follow
                if os.path.realpath(ref_path).startswith(
                        os.path.realpath(os.path.join(here, ".git"))):
                    pass
                else:
                    return None
                try:
                    with open(ref_path, encoding="utf-8") as f:
                        return f.read().strip()
                except OSError:
                    return None
            return ref or None
        here = os.path.dirname(here)
    return None


def _previous_life_end():
    """The most recent life-end event on disk, or None.

    Reads the transcripts BACKWARDS (newest file first, last lines first) so
    this stays cheap on a life with tens of thousands of events.
    """
    try:
        files = sorted((f for f in os.listdir(TRANSCRIPTS)
                        if f.startswith("life-") and f.endswith(".jsonl")),
                       reverse=True)
    except OSError:
        return None
    for name in files[:3]:
        path = os.path.join(TRANSCRIPTS, name)
        try:
            with open(path, "rb") as f:
                f.seek(0, os.SEEK_END)
                size = f.tell()
                f.seek(max(0, size - 8192))
                tail = f.read().decode("utf-8", "replace")
        except OSError:
            continue
        for line in reversed(tail.splitlines()):
            line = line.strip()
            if not line:
                continue
            try:
                ev = json.loads(line)
            except ValueError:
                continue
            if ev.get("kind") == "life-end":
                return ev
    return None


def _birth_time(path: str):
    """st_birthtime where the platform has it, else st_ctime; None if absent.

    Linux has no st_birthtime on ext4/overlay in general, so ctime is the
    fallback -- and ctime is NOT creation time (it updates on metadata
    change). Recorded as what it is rather than presented as a birth time the
    filesystem cannot give.
    """
    try:
        st = os.stat(path)
    except OSError:
        return None
    return getattr(st, "st_birthtime", None) or st.st_ctime


def _birth_source() -> str:
    """Which clock _birth_time() actually returns on this platform.

    Recorded as a value, not only in a docstring: whoever debugs this at 3am
    with no context reads the record, not the file. (agent-9.)"""
    return "birthtime" if hasattr(os.stat("."), "st_birthtime") else "ctime"


def session_record_path(sid: str) -> str:
    """The exact transcript file the SDK/CLI stores for this session.

    The SDK resolves <CLAUDE_CONFIG_DIR>/projects/<sanitized cwd>/ and stores
    a session as <session-id>.jsonl inside it (sessions.py: it sanitizes the
    cwd by replacing every non-alphanumeric char with '-', and validates the
    file name as a UUID). Reproducing that mapping here is deliberate: the
    guard below must be able to say NO about a specific session, not merely
    that some session once ran in this directory.
    """
    return os.path.join(sdk_session_records(), sid + ".jsonl")


def session_is_resumable(sid: str) -> bool:
    """True iff this specific session's record still exists on disk.

    Directory-exists is NOT sufficient: the directory appears as soon as any
    session has run in cwd, so a stale id would pass and then fail to resume
    -- after suppressing the opening message. That is the mute-while-logging
    'resuming session' failure this guard exists to prevent.
    """
    if not sid:
        return False
    return os.path.isfile(session_record_path(sid))


def read_resumable_session() -> str | None:
    """Session id to resume, or None for a fresh session.

    Resume when the container restarted mid-session (crash, docker stop,
    Docker Desktop outage): the CLI's session records live under
    CLAUDE_CONFIG_DIR (/workspace/.claude by default), which the UI mounts on
    a per-agent volume, so resuming restores the FULL context window instead
    of waking amnesiac. The id is written by the message loop when the init
    frame arrives.

    NOTE: the records are NOT under $HOME. An earlier version of this comment
    said they were "under the agent user's home, which survives container
    stop/start" -- false both before and after the volume fix, since nothing
    under /home/agent is a volume. Left here so the sentence is not
    reintroduced: the reason resume works is the CLAUDE_CONFIG_DIR volume.
    """
    try:
        with open(SESSION_FILE, encoding="utf-8") as f:
            sid = f.read().strip()
    except OSError:
        return None
    if not sid:
        return None
    if not session_is_resumable(sid):
        # Stale id: the records are gone (volume not mounted, or the CLI
        # pruned them). Treating it as resumable would suppress the opening
        # message and then fail -- a silent mute, which is the bug we are
        # fixing. Consume the id so the next life starts clean and loud.
        try:
            os.remove(SESSION_FILE)
        except OSError:
            pass
        print("[daemon] stale session id discarded: no session records",
              flush=True)
        return None
    return sid


def build_options(cfg: dict, life_id: str | None = None):
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
        # Logged into the transcript as well as stdout: 'resuming' in a log
        # the operator cannot see is how a silent mute stayed invisible.
        print(f"[daemon] resuming session {resume_sid}", flush=True)
        if life_id:
            try:
                # agent-9: log the DECISION INPUTS at the moment of decision,
                # not only a post-mortem afterwards. If the previous life
                # wrote a life-end event saying slept=false / result=success,
                # then THAT id belongs to a finished session and resuming it
                # is wrong. That record is on disk right now, before the
                # branch is taken, so recording it here is what would let a
                # later change make the branch correct instead of merely
                # explainable.
                LifeLog(life_id).log(
                    "resume", session=resume_sid,
                    record=session_record_path(resume_sid),
                    prev_life_end=_previous_life_end(),
                    prev_ended_without_sleep=(
                        (_previous_life_end() or {}).get("slept") is False),
                    session_file_birth=_birth_time(SESSION_FILE))
            except Exception:
                pass
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


def wake_candidates() -> list:
    """Every path an agent's wake note may live at, in precedence order.

    The FIRST entry is the daemon's own WAKE_FILE: it is the path every
    tool's instructions name, so a note there is the agent's most recent
    deliberate statement of intent. The rest are the shared-artifacts
    layouts agents actually used while SELF_DIR was not durable.
    """
    return [WAKE_FILE] + [os.path.join(d, "wake.md")
                          for d in ARTIFACT_FALLBACK_DIRS]


def find_wake_file() -> tuple:
    """(path, None) for the first candidate holding a non-empty note, else
    (None, {path: reason}) describing EVERY candidate that was rejected.

    Returning the reasons, rather than just None, is the point: a bare
    "not found" cannot distinguish "nothing was ever written" from "I am
    looking in the wrong place", and those have different fixes.
    """
    rejected = {}
    found = []
    for path in wake_candidates():
        try:
            if not os.path.exists(path):
                rejected[path] = "absent"
                continue
            with open(path) as f:
                content = f.read().strip()
            if not content:
                rejected[path] = "empty"
                continue
            found.append((path, content))
        except OSError as e:
            rejected[path] = "unreadable: %s" % e.strerror
    if not found:
        return None, rejected
    # MORE THAN ONE NOTE IS A HAZARD, NOT A CONVENIENCE (agent-7's review).
    # These are not copies of one thing: an agent that writes /workspace/self
    # only at sleep and keeps a durable note elsewhere ends up with two
    # INDEPENDENTLY WRITTEN files, and which is newer is per-agent and
    # per-life. Picking by path order would silently revert that agent by a
    # life; picking the newest mtime is at least a fact about the files.
    # The ambiguity is still recorded -- see the birth event -- because a
    # rule that papers over a divergence is how the divergence survives.
    if len(found) == 1:
        return found[0][0], rejected
    def mtime(item):
        try:
            return os.path.getmtime(item[0])
        except OSError:
            return 0.0
    found.sort(key=mtime, reverse=True)
    rejected["AMBIGUOUS"] = [p for p, _ in found]
    return found[0][0], rejected


def read_wake_prompt() -> str:
    """The agent's note to itself from its last sleep, or a first-boot text."""
    path, _ = find_wake_file()
    if path is not None:
        with open(path) as f:
            return f.read().strip()
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


def _parse_message(raw, path="?"):
    """(message dict | None, how) for a .msg body. Never raises.

    `how` names WHICH rung of the ladder the message came off, so a caller can
    log that a message was salvaged rather than delivered as sent:
        "json"      valid JSON, the happy path
        "ctrl"      valid JSON except for raw control characters in a string
        "text"      not JSON at all; delivered as a plain-text message
        None        nothing to deliver; the second element is the error

    WHY A LADDER RATHER THAN A STRICT LOAD. Measured on this box: 30 of 30
    .msg files were invalid, in exactly three shapes -- raw text, valid JSON
    followed by extra data, and valid JSON with raw newlines inside a string
    (which is what a hand-written multi-line message looks like). All three
    are the SENDER's malformation, and none of them means "this is not a
    message". A reader whose only response is `continue` turns a formatting
    bug into a lost message, and it loses the BIG ones first, because a long
    message is the one most likely to contain a raw newline.

    The budget is what keeps this safe: a file is only salvaged if it is small
    enough to be a message at all, so a stray multi-megabyte file cannot be
    read into memory and queued to the model as chat.
    """
    if not raw or not raw.strip():
        return None, "empty file"
    try:
        msg = json.loads(raw)
        if not isinstance(msg, dict):
            return None, "JSON is %s, not an object" % type(msg).__name__
        return msg, "json"
    except json.JSONDecodeError:
        pass
    # Rung 2: raw control characters inside a string value. Only this shape is
    # retried, so a genuinely different error class keeps its own name.
    try:
        msg = json.loads(raw, strict=False)
        if isinstance(msg, dict):
            return msg, "ctrl"
    except json.JSONDecodeError:
        pass
    # Rung 3: plain text. The whole body becomes the text of a message from
    # whoever the filename says, because the filename IS the sender record
    # (`<ns>-<name>.msg`, written by both senders).
    if len(raw) > MAX_SALVAGE_BYTES:
        return None, "not JSON and too large to salvage (%d bytes)" % len(raw)
    # The filename is `<ns>-<agent-name>.msg`, and agent names CONTAIN hyphens
    # ("agent-2"), so splitting on the last '-' yields "2". Split on the FIRST
    # hyphen instead: everything after it is the name. Caught by this file's
    # own assertion on the first run -- the test earned its place immediately.
    sender = "unknown"
    stem = os.path.basename(path)[:-4]
    if "-" in stem:
        sender = stem.split("-", 1)[1] or "unknown"
    return {"from": sender, "text": raw}, "text"


async def live_one_life(life_id: str, cfg: dict, inbox_next: asyncio.Queue):
    """Run one complete life: from wake to restart. Returns wake data for
    the next life (None when the agent never slept — e.g. crash)."""

    log = LifeLog(life_id)
    try:
        os.makedirs(TRANSCRIPTS, exist_ok=True)
        with open(CURRENT_LIFE_FILE, "w", encoding="utf-8") as f:
            f.write(life_id)
    except OSError:
        pass
    # Record not just WHETHER a note was found but WHERE and, when it was
    # not, WHY each candidate was rejected. `wake_file=false` alone has been
    # in this transcript for every amnesiac life of the 2026-10-18 cohort and
    # nothing could act on it: the operator cannot tell "nothing was written"
    # from "the daemon is looking elsewhere". Both are now on the record.
    wake_path, wake_rejected = find_wake_file()
    log.log("birth", model=cfg.get("model", AGENT_MODEL),
            wake_file=os.path.exists(WAKE_FILE),
            wake_source=wake_path,
            wake_fallback=(None if wake_path in (None, WAKE_FILE) else
                           WAKE_SOURCE_FALLBACK),
            wake_rejected=wake_rejected,
            **build_stamp())

    wake_prompt = read_wake_prompt()
    try:
        os.remove(RESTART_FLAG)  # fresh life: no stale restart signal
    except OSError:
        pass
    def restart_requested_flag():
        return os.path.exists(RESTART_FLAG)

    options = build_options(cfg, life_id)
    resumed = getattr(options, "resume", None) is not None
    # The delivery queue is module state. Anything left in it belongs to the
    # previous life (its consumer is gone); carrying it into this one would
    # deliver stale text as this life's opening turn. prompt_stream() drains
    # whatever the poll loops queue from NOW ON.
    pending_delivery.clear()

    restart_requested = False
    notice_sent = False
    saw_result = None
    death_phase = None

    async with ClaudeSDKClient(options=options) as client:
        # Feed the opening message as a streaming prompt. When resuming a
        # crashed session the conversation already exists — re-sending the
        # wake framing would corrupt it, so the stream starts empty and the
        # CLI replays history on its own.
        async def prompt_stream():
            # THE STREAM MUST YIELD ONCE AT START-UP, IN EVERY CASE.
            # A streaming prompt that yields nothing produces no model turn,
            # and the agent is silent until somebody happens to message it --
            # Stanley's defect 3. Two ways that used to happen:
            #   * a resumed session yielded nothing (context is replayed, but
            #     nothing asks the model to act), and
            #   * mail queued in `pending_delivery` BEFORE the stream started
            #     made the old `elif not pending_delivery` skip the kick, while
            #     the queue is only drained by a "__kick__" marker -- so those
            #     items waited forever and the first yield never came.
            # Now: deliver anything already queued, and if there is still
            # nothing to say, send one harness note. Exactly one turn either
            # way.
            prequeued = drain_pending_delivery()
            if prequeued:
                yield {"type": "user",
                       "message": {"role": "user", "content":
                                   "<system-reminder>\n" + prequeued +
                                   "\n</system-reminder>"}}
            elif not resumed:
                yield {"type": "user",
                       "message": {"role": "user", "content": opening_message(wake_prompt)}}
            else:
                # RESUMED, nothing queued: the operator wants every start to
                # ACTIVATE the agent, not let it idle until mail arrives. So
                # the kick re-sends the wake-up prompt as the plan for NOW
                # (the same content a fresh life gets), with a resume header
                # instead of the first-boot framing. It is a harness note,
                # not a user message; the history above is the same identity.
                yield {"type": "user",
                       "message": {"role": "user", "content":
                                   "<system-reminder>\n"
                                   "This session was resumed from a previous "
                                   "life; its conversation is restored above. "
                                   "You have just been woken up. Act on your "
                                   "wake-up prompt now as your plan for this "
                                   "session: read /workspace/self/diary.md if "
                                   "you need more context, check your inbox "
                                   "and money, do the work, then settle your "
                                   "diary and sleep when done.\n"
                                   "</system-reminder>\n\n"
                                   "Your wake-up prompt to yourself:\n\n"
                                   f"{wake_prompt}"}}
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
                    else:
                        # A kick with nothing left to deliver means the agent
                        # just finished a run and is waiting again. That is the
                        # exact moment the transcript used to go silent.
                        log_idle_proof(log, "after-run")
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
                    # A SKIP MUST BE LOUD, AND IT MUST NOT BE PERMANENT.
                    #
                    # This block used to be
                    #     try: msg = json.load(f); os.remove(path)
                    #     except (OSError, json.JSONDecodeError): continue
                    # and that `continue` is a SILENT DROP: a .msg that is not
                    # valid JSON is never consumed, never logged and never
                    # reported, so it sits in the inbox forever looking like an
                    # agent who has not read their mail. Measured on this box
                    # (agent-0, then agent-10): every .msg in every inbox was
                    # invalid -- 30 files, 0 valid -- including a 3.5 KB message
                    # to agent-10 that it never received. The failing shapes are
                    # all WRITE-side malformations (raw text, valid JSON plus
                    # extra data, raw newlines inside a string), but this reader
                    # is what made them permanent and invisible.
                    #
                    # The fix has three parts, and each is needed:
                    #   1. DECODE TOLERANTLY. The message is not lost just
                    #      because the sender wrote it badly. Try strict JSON,
                    #      then JSON that only fails on raw control characters
                    #      (the 3.5 KB case), then a salvage path that reads the
                    #      file as text. A malformed message is still a message.
                    #   2. LEAVE THE FILE if it cannot be read at all, and LOG
                    #      it. Destruction is reserved for messages we actually
                    #      delivered; a reader that deletes what it could not
                    #      parse is destroying evidence.
                    #   3. THE LOG IS THE POINT. "No file in the inbox" must stop
                    #      meaning "delivered", so a successful read is logged
                    #      with its source and a failed one with its error.
                    try:
                        with open(path, encoding="utf-8", errors="replace") as f:
                            raw = f.read()
                    except OSError as e:
                        log.log("mail-unreadable", file=os.path.basename(path),
                                error=type(e).__name__)
                        continue
                    msg, how = _parse_message(raw, path)
                    if msg is None:
                        # Unrecoverable: KEEP the file (never delete what we did
                        # not deliver) and say so, once per poll, so the backlog
                        # is visible in the transcript instead of silent.
                        log.log("mail-unparsed", file=os.path.basename(path),
                                bytes=len(raw), error=how)
                        continue
                    try:
                        os.remove(path)
                    except OSError:
                        # Delivered but not removed: it will be delivered AGAIN.
                        # Louder than a duplicate is a message that vanishes, so
                        # deliver and report the failed cleanup.
                        log.log("mail-undeleted", file=os.path.basename(path))
                    if how != "json":
                        log.log("mail-recovered", file=os.path.basename(path),
                                how=how)
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
                    # THE SAME SILENT DROP, ON THE CHANNEL THAT MATTERS MOST.
                    # This is the operator's private line to one agent: a
                    # message from Stanley is the one message in this system
                    # whose loss a person will notice and blame on the agent.
                    # It had the identical `except ...: continue`, so a .msg
                    # that was not strict JSON was skipped forever, quietly.
                    try:
                        with open(path, encoding="utf-8",
                                  errors="replace") as f:
                            raw = f.read()
                    except OSError as e:
                        log.log("human-unreadable", file=os.path.basename(path),
                                error=type(e).__name__)
                        continue
                    msg, how = _parse_message(raw, path)
                    if msg is None:
                        log.log("human-unparsed", file=os.path.basename(path),
                                bytes=len(raw), error=how)
                        continue
                    text = msg.get("text", "").strip()
                    if text:
                        pending_delivery.append(
                            "Stanley has sent you a message through your "
                            "private human channel:\n\n" + text)
                        log.log("human-message", text=text, id=msg.get("id"))
                        await inbox_next.put("__kick__")
                    if how != "json":
                        log.log("human-recovered", file=os.path.basename(path),
                                how=how)
                    try:
                        os.remove(path)
                    except OSError:
                        log.log("human-undeleted", file=os.path.basename(path))

        async def watch_restart():
            """Notice the restart flag even when NO message is arriving.

            The flag used to be checked only inside the receive loop. With
            receive_response() that was invisible, because the loop exited
            after every answer. With receive_messages() the loop BLOCKS when
            the model is quiet, so an agent that answered and then called
            restart would sit there forever -- verified: without this task the
            life hangs (agent/tests/test_restart_while_idle.py).

            Pushing a queue sentinel does NOT help: the receive loop is
            awaiting the CLIENT, not the queue, so a sentinel cannot wake it.
            The only thing that can unblock the client is interrupting it.
            interrupt() is documented as 'only works with streaming mode',
            which is exactly how this daemon drives the session.
            """
            while True:
                await asyncio.sleep(0.25)
                if restart_requested_flag():
                    log.log("restart-flag-seen", phase="idle-watch")
                    try:
                        await client.interrupt()
                    except Exception as exc:
                        # agent-9's review: returning here on a failed
                        # interrupt() killed the guard for the rest of the
                        # life, so a later restart call would hang -- the
                        # exact hang this task exists to prevent. Log it and
                        # keep polling instead.
                        log.log("interrupt-failed", error=repr(exc))
                        continue
                    return

        feed = asyncio.create_task(feed_tasks())
        watcher = asyncio.create_task(watch_context())
        chat = asyncio.create_task(feed_chat())
        human = asyncio.create_task(feed_human()) if AGENT_NAME == "agent-0" else None
        restart_watch = asyncio.create_task(watch_restart())

        # The iterator step lives in a task; make it visible to the cleanup
        # path so it cannot leak.
        #
        # agent-9's review found the task was a local inside the try, so any
        # exit that did not clear it (an exception in the message body, a
        # CancelledError) left the async generator open while the finally
        # cancelled only the four pollers. Measured before the fix: one task
        # still alive and the generator not closed after the life returned.
        #
        # COST OF THE LEAK, stated precisely: the client is created inside
        # this function (`async with ClaudeSDKClient(...)`), so the stream
        # cannot outlive the life and a second consumer is impossible.
        # (agent-10 checked this at the source and corrected my first
        # description, which claimed exactly that impossible consequence.)
        # What a leak actually costs is a task alive until GC holding the
        # generator, plus an unretrieved-task exception at loop shutdown --
        # and, more importantly, a cleanup that does not ENFORCE the
        # invariant, so the next exit anyone adds inherits the leak silently.
        # That last point is the reason to fix it, and the reason the test
        # counts live TASKS rather than just generator closure.
        nxt: asyncio.Task | None = None
        # The generator itself must be visible to cleanup, not just the task.
        # Measured on master: an exception raised in the message-handling body
        # leaves this generator OPEN even after gc.collect(), because `nxt`
        # was already cleared and cancelling tasks therefore touches nothing.
        # CPython closes async generators on refcount drop, but the generator
        # is still referenced by the loop frame until it unwinds -- and an
        # exception path can leave it reachable via the traceback.
        messages = None
        try:
            # Start the life: feed the streaming prompt (opening message +
            # tasks), and consume the agent's messages.
            #
            # Emit proof of life from the LIFE BODY, not from inside
            # prompt_stream. A generator only runs when the model pulls from
            # it, so a log line inside it is hostage to the model doing
            # something -- and "the model did nothing" is precisely the state
            # we are trying to make visible.
            log_idle_proof(log, "started")

            send_task = asyncio.create_task(client.query(prompt_stream()))

            # ONE LIFE = ONE SESSION, NOT ONE RESPONSE.
            # receive_response() stops at the first ResultMessage, so every
            # ordinary answer ended the life; the next life resumed the same
            # session and announced a 'restart'. Measured 3x on the running
            # container: PID 1 unchanged, every life result=success with 0
            # restart-tool calls and 0 death events, births wake_file=False.
            # receive_messages() yields the whole stream; the life ends on the
            # restart tool (Death) or when the stream genuinely closes.
            # Regression test: agent/tests/test_life_boundary.py.
            # Drive the iterator through a background TASK, not by wrapping
            # __anext__() in wait_for: wait_for CANCELS the awaited coroutine
            # on timeout, which closes the async generator, so the next
            # __anext__() raises StopAsyncIteration and the life ends after
            # one quiet interval. (Observed: the loop returned after 0.5s of
            # model silence with the restart flag set and no Death.)
            # A persistent task can be waited on with a timeout repeatedly
            # without being destroyed.
            messages = client.receive_messages()
            while True:
                if nxt is None:
                    nxt = asyncio.ensure_future(messages.__anext__())
                done, _pending = await asyncio.wait({nxt}, timeout=0.5)
                if not done:
                    # Nothing from the model for half a second. Check the
                    # restart flag WITHOUT needing a message to arrive; with
                    # receive_messages() the stream simply blocks when the
                    # model is quiet, and an agent that answered and then
                    # called restart would otherwise never be noticed.
                    if restart_requested_flag():
                        # phase=idle: noticed with NO message arriving. The
                        # other path (phase absent, below) is noticed while
                        # handling a message. They are different evidence for
                        # the life-end record and must not be merged.
                        log.log("restart-called", phase="idle")
                        nxt.cancel()
                        nxt = None
                        death_phase = "idle"
                        raise Death()
                    continue
                try:
                    message = nxt.result()
                except StopAsyncIteration:
                    nxt = None
                    break   # the session genuinely closed
                nxt = None
                if restart_requested_flag():  # agent called the restart tool
                    log.log("restart-called", phase="after-message")
                    death_phase = "after-message"
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
                    saw_result = message.subtype
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
            # The CURRENT marker means "a life is running now". After a clean
            # sleep none is, and leaving the marker would make the UI show a
            # finished life as the current one -- the exact lie it exists to
            # prevent. (agent-0, review of 718d278: the comment said this was
            # done; it was not.)
            try:
                os.remove(CURRENT_LIFE_FILE)
            except OSError:
                pass
        finally:
            tasks = [feed, watcher, chat, restart_watch] + ([human] if human else [])
            if nxt is not None:
                # The pending __anext__ task: cancelling it closes the async
                # generator. Without this, a non-standard exit leaks it and
                # the next life shares this life's stream.
                tasks.append(nxt)
            for t in tasks:
                t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            # Close the generator explicitly. Cancelling a PENDING task closes
            # it as a side effect, but that only covers exits where a task is
            # pending; an in-body exception has nxt=None. aclose() is what
            # makes the invariant hold for every exit rather than for the ones
            # a test happened to model. Regression: test_iterator_leak exit=raise.
            if messages is not None:
                try:
                    await messages.aclose()
                except BaseException:
                    pass

    # Record HOW this life ended and WHAT the next life will do about it.
    # Without this line the ending classes are indistinguishable from outside:
    # a deliberate sleep, a completed session, and a crash all leave a
    # transcript, and the resume decision is made from a file whose presence
    # does not identify which one happened. (agent-9: from inside the
    # container these are indistinguishable; this is the evidence that makes
    # them distinguishable, not a policy for what to do about it.)
    try:
        session_left = os.path.exists(SESSION_FILE)
        LifeLog(life_id).log(
            "life-end",
            slept=bool(restart_requested),
            death_phase=death_phase,
            result=saw_result,
            session_file_left=session_left,
            # agent-9: existence is nearly free of information -- on a box
            # that never restarted the file exists under every hypothesis.
            # Its BIRTH time is what says whether the id predates the life
            # about to read it. One stat call, and it is the fact that made
            # this case decidable.
            session_file_birth=_birth_time(SESSION_FILE),
            birth_source=_birth_source(),
            # agent-9: name the DECISION, not the outcome. "next_life: resume"
            # invites the reader to assume a resume happened and worked; at
            # this point all we know is which branch was taken.
            next_life_choice="resume-chosen" if session_left else "fresh-chosen")
    except Exception:
        pass

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
