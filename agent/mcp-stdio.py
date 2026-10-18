"""Stdio MCP server for the agent daemon: exposes `restart`, `chat`, and
`list_peers` as real tools the CLI loads via --mcp-config.

Why stdio, not the SDK's in-process {"type": "sdk"} server: this CLI
generation (2.1.30x) expects a manifest handshake the Python SDK 0.2.168
doesn't send, so sdk-type servers are silently never contacted. Stdio
servers load reliably (verified by JSON-RPC handshake and by the agent
seeing the tools).

Signals:
  - restart: writes /tmp/restart-requested (daemon's message loop polls
    this file and ends the life when it appears).
  - chat: writes JSON into the shared mailbox volume directly — same
    wire format the daemon's feed_chat() already consumes.

Usage (from the daemon):
  mcp_servers={"life": {"type": "stdio",
                        "command": "python3",
                        "args": ["/app/mcp_stdio.py"],
                        "env": {"AGENT_NAME": <name>}}}
"""

import json
import os
import sys
import time

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import (
    CallToolRequest,
    CallToolResult,
    ListToolsRequest,
    ListToolsResult,
    TextContent,
    Tool,
)

RESTART_FLAG = "/tmp/restart-requested"
SHARED = "/shared"
MAILBOX = os.path.join(SHARED, "mailbox")


def peers(my_name):
    try:
        return sorted(d for d in os.listdir(MAILBOX)
                      if os.path.isdir(os.path.join(MAILBOX, d)) and d != my_name)
    except OSError:
        return []


def main():
    my_name = os.environ.get("AGENT_NAME", "agent")
    server = Server("life-cycle")

    async def handle_list_tools(ctx, params) -> ListToolsResult:
        return ListToolsResult(
            tools=[
                Tool(
                    name="restart",
                    description=(
                        "Sleep and start a fresh session. Call this when "
                        "your context is running low and you have settled "
                        "your work: finish anything quick, update your "
                        "diary at /workspace/self/diary.md, and write "
                        "your wake-up prompt to /workspace/self/wake.md "
                        "(instructions from your past self to your next "
                        "session), then call this. Everything not saved "
                        "in files is lost when you sleep."
                    ),
                    inputSchema={"type": "object", "properties": {}},
                ),
                Tool(
                    name="chat",
                    description=(
                        f"Send a message to another agent in your group. "
                        f"You are '{my_name}'. The recipient is the "
                        "agent's name (see list_peers). Delivery is "
                        "asynchronous; replies arrive as system "
                        "messages. Continue your own work meanwhile."
                    ),
                    inputSchema={
                        "type": "object",
                        "properties": {
                            "recipient": {"type": "string"},
                            "message": {"type": "string"},
                        },
                        "required": ["recipient", "message"],
                    },
                ),
                Tool(
                    name="list_peers",
                    description=(
                        "List the other agents currently in the group."
                    ),
                    inputSchema={"type": "object", "properties": {}},
                ),
            ],
        )

    async def handle_call_tool(ctx, params) -> CallToolResult:
        name = params.name
        args = params.arguments or {}
        if name == "list_peers":
            found = peers(my_name)
            return CallToolResult(content=[TextContent(
                type="text",
                text=", ".join(found) if found
                else "no other agents yet",
            )])
        if name == "chat":
            recipient = str(args.get("recipient", "")).strip()
            message = str(args.get("message", "")).strip()
            if not recipient or not message:
                return CallToolResult(content=[TextContent(
                    type="text", text="error: recipient and message required")])
            if recipient == my_name:
                return CallToolResult(content=[TextContent(
                    type="text", text="error: that's your own name")])
            inbox = os.path.join(MAILBOX, recipient, "incoming")
            try:
                os.makedirs(inbox, exist_ok=True)
                fname = os.path.join(inbox, f"{time.time_ns()}-{my_name}.msg")
                with open(fname, "w") as f:
                    json.dump({"from": my_name, "text": message,
                               "time": time.strftime("%H:%M:%S")}, f)
            except OSError as e:
                return CallToolResult(content=[TextContent(
                    type="text", text=f"error: {e}")])
            return CallToolResult(content=[TextContent(
                type="text",
                text=f"Message delivered to {recipient}'s mailbox; "
                     "it will reach them at their next safe point.")])
        if name == "restart":
            with open(RESTART_FLAG, "w") as f:
                f.write(str(time.time()))
            return CallToolResult(content=[TextContent(
                type="text",
                text="Restart requested. The session ends now. Your next "
                     "session begins fresh, opened with the wake-up "
                     "prompt you wrote to /workspace/self/wake.md.",
            )])
        raise ValueError(f"unknown tool: {name}")

    server.add_request_handler(
        "tools/list", ListToolsRequest, handle_list_tools)
    server.add_request_handler(
        "tools/call", CallToolRequest, handle_call_tool)

    import anyio

    async def serve():
        async with stdio_server() as (read, write):
            await server.run(read, write, server.create_initialization_options())

    anyio.run(serve)


if __name__ == "__main__":
    main()
