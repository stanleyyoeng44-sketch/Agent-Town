"""Run a Claude Code agent inside the container, driven by the Claude Agent SDK.

The agent:
  - works in /workspace (a normal writable bind mount of a host folder);
  - is served by the host's CodeBuddy proxy at http://127.0.0.1:8787
    (reachable because the container runs with --network host);
  - uses model glm-5.3;
  - cannot modify this script: /app is mounted read-only by docker run.

Usage inside the container (that is what ENTRYPOINT runs):
    python3 /app/agent-run.py "do something in my workspace"

    # or pipe the task on stdin:
    echo "do something" | python3 /app/agent-run.py

Optional MCP servers: mount a JSON config at /app/mcp.json (read-only).
Format = {"servers": {"<name>": {"command": ..., "args": [...], "env": {...}}}}
(the "servers" wrapper matches the Claude Code .mcp.json format).
"""

import asyncio
import json
import os
import sys
import time

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ResultMessage,
    SystemMessage,
    TextBlock,
    UserMessage,
    query,
)

APP_DIR = "/app"
WORKSPACE = "/workspace"
MCP_CONFIG = os.path.join(APP_DIR, "mcp.json")

# Reaches the host's CodeBuddy proxy. The container runs on Docker Desktop,
# where --network host is the VM's network, not the real machine's — so the
# host is addressed as host.docker.internal instead of 127.0.0.1. Docker
# Desktop forwards the connection to the host's loopback, which the proxy
# (bound to 127.0.0.1 only) accepts. Override with PROXY_URL env if needed.
PROXY_URL = os.environ.get(
    "PROXY_URL", "http://host.docker.internal:8787"
)

# Proxy + model credentials, passed down to the CLI subprocess.
ENV = {
    "ANTHROPIC_BASE_URL": PROXY_URL,
    "ANTHROPIC_AUTH_TOKEN": "local-proxy",
    "ANTHROPIC_MODEL": "glm-5.3",
    # HOME is set by the entrypoint when dropping to the 'agent' user;
    # setting it here too keeps direct runs (without the entrypoint) working.
    "HOME": "/home/agent",
    "DISABLE_TELEMETRY": "1",
    "DISABLE_ERROR_REPORTING": "1",
}


def read_task() -> str:
    """Task text comes from argv, or the whole of stdin if piped."""
    if len(sys.argv) > 1:
        return " ".join(sys.argv[1:])
    if not sys.stdin.isatty():
        return sys.stdin.read()
    print("Give the agent a task: agent-run \"your task here\"", file=sys.stderr)
    sys.exit(2)


def load_mcp_servers():
    """Optional MCP config, mounted read-only at /app/mcp.json."""
    if not os.path.exists(MCP_CONFIG):
        return None
    with open(MCP_CONFIG) as f:
        data = json.load(f)
    # Accept both {"servers": {...}} (Claude Code .mcp.json) and a bare dict.
    return data.get("servers", data)


async def run(task: str):
    # Transcript: one JSON object per line, in the workspace so it survives
    # the container (which runs with --rm). Tool calls and results included.
    os.makedirs(WORKSPACE, exist_ok=True)
    ts = time.strftime("%Y%m%d-%H%M%S")
    transcript_path = os.path.join(WORKSPACE, f"transcript-{ts}.jsonl")

    def log(kind: str, **fields):
        entry = {"time": time.strftime("%H:%M:%S"), "kind": kind, **fields}
        with open(transcript_path, "a") as f:
            f.write(json.dumps(entry, default=str) + "\n")

    log("session", task=task, model="glm-5.3")
    print(f"[transcript: {transcript_path}]", file=sys.stderr)

    options = ClaudeAgentOptions(
        model="glm-5.3",
        cwd=WORKSPACE,
        # Inside a throwaway container the agent owns everything anyway;
        # bypassPermissions just stops it waiting on prompts nobody answers.
        permission_mode="bypassPermissions",
        env=ENV,
        mcp_servers=load_mcp_servers() or {},
        # Don't read settings files that don't exist inside the container;
        # everything the agent needs is set explicitly here.
        setting_sources=[],
    )

    async for message in query(prompt=task, options=options):
        if isinstance(message, AssistantMessage):
            for block in message.content:
                if isinstance(block, TextBlock):
                    log("assistant", text=block.text)
                    print(block.text, flush=True)
                else:
                    # Tool use / thinking blocks: record raw.
                    log("assistant-block",
                        block_class=type(block).__name__,
                        **({"name": block.name, "input": block.input}
                           if hasattr(block, "name") else {}))
        elif isinstance(message, UserMessage):
            for block in message.content:
                if not isinstance(block, TextBlock):
                    # Tool results the agent received.
                    log("tool-result", content=getattr(block, "content", str(block)))
        elif isinstance(message, SystemMessage):
            log("system", subtype=getattr(message, "subtype", ""))
        elif isinstance(message, ResultMessage):
            log("result", subtype=message.subtype,
                turns=message.num_turns, cost=message.total_cost_usd)
            if message.subtype == "success":
                print(
                    f"\n[done] turns: {message.num_turns}, "
                    f"cost: ${message.total_cost_usd}",
                    file=sys.stderr,
                )
            else:
                print(f"\n[{message.subtype}] {message.error or ''}", file=sys.stderr)


if __name__ == "__main__":
    try:
        asyncio.run(run(read_task()))
    except KeyboardInterrupt:
        sys.exit(130)
