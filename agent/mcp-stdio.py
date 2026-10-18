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
import re
import sys
import time
import urllib.error
import urllib.request
import uuid

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import (
    CallToolRequestParams,
    CallToolResult,
    ListToolsResult,
    PaginatedRequestParams,
    TextContent,
    Tool,
)

RESTART_FLAG = "/tmp/restart-requested"
SHARED = "/shared"
MAILBOX = os.path.join(SHARED, "mailbox")
HUMAN_OUTBOX = "/workspace/human/outbox"
BANK_URL = os.environ.get("BANK_URL", "http://host.docker.internal:8790")
AGENT_NAME_RE = re.compile(r"^(?:agent-[0-9]+|glm-agent)$")


def peers(my_name):
    try:
        return sorted(d for d in os.listdir(MAILBOX)
                      if AGENT_NAME_RE.fullmatch(d)
                      and os.path.isdir(os.path.join(MAILBOX, d))
                      and d != my_name)
    except OSError:
        return []


def main():
    my_name = os.environ.get("AGENT_NAME", "agent")
    # Only agent-0 exposes the human-facing tool. The UI reads the
    # delegate's private workspace, never the peer-shared volume.
    is_delegate = my_name == "agent-0"
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
                Tool(
                    name="check_money",
                    description=(
                        "Check how many coins you have in your bank "
                        "account. Your account automatically loses 1 coin "
                        "per minute; transfers move coins between agents."
                    ),
                    inputSchema={"type": "object", "properties": {}},
                ),
                Tool(
                    name="send_money",
                    description=(
                        f"Transfer coins from your account to another "
                        f"agent's. You are '{my_name}'. The amount must "
                        "be a positive integer you can afford; the bank "
                        "rejects overdrafts. The recipient is notified "
                        "through their mailbox. Money is scarce: you "
                        "lose 1 coin per minute, so spend deliberately."
                    ),
                    inputSchema={
                        "type": "object",
                        "properties": {
                            "recipient": {
                                "type": "string",
                                "description": "the receiving agent's name",
                            },
                            "amount": {
                                "type": "integer",
                                "description": "coins to send (>= 1)",
                            },
                        },
                        "required": ["recipient", "amount"],
                    },
                ),
            ] + ([Tool(
                name="talk_to_stanley",
                description=(
                    "Send a message to Stanley, the human operator. "
                    "Stanley reads it in the UI. Use this to report to "
                    "them, ask them things, or relay what the agents are "
                    "doing. Their replies arrive as messages from "
                    "Stanley. This is your ONLY channel to "
                    "Stanley — no other agent has one."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "message": {
                            "type": "string",
                            "description": "what to say to Stanley",
                        }
                    },
                    "required": ["message"],
                },
            )] if is_delegate else [])
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
            if not AGENT_NAME_RE.fullmatch(recipient):
                return CallToolResult(content=[TextContent(
                    type="text", text="error: recipient must be an agent")])
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
        if name == "check_money":
            try:
                with urllib.request.urlopen(
                        f"{BANK_URL}/balance/{my_name}", timeout=10) as r:
                    data = json.loads(r.read())
                return CallToolResult(content=[TextContent(
                    type="text",
                    text=f"You have {data['money']} coins.",
                )])
            except urllib.error.HTTPError as e:
                return CallToolResult(content=[TextContent(
                    type="text", text=f"bank error: {e.code}")])
            except OSError as e:
                return CallToolResult(content=[TextContent(
                    type="text", text=f"bank unreachable: {e}")])
        if name == "send_money":
            recipient = str(args.get("recipient", "")).strip()
            amount = args.get("amount")
            if (isinstance(amount, bool) or not isinstance(amount, int)
                    or amount < 1):
                return CallToolResult(content=[TextContent(
                    type="text", text="error: amount must be a positive integer")])
            if recipient == my_name:
                return CallToolResult(content=[TextContent(
                    type="text", text="error: cannot send money to yourself")])
            payload = json.dumps({"from": my_name, "to": recipient,
                                  "amount": amount}).encode()
            headers = {"Content-Type": "application/json"}
            if is_delegate:
                try:
                    with open("/app/agent0-transfer-token", encoding="utf-8") as f:
                        headers["X-Agent0-Token"] = f.read().strip()
                except OSError:
                    return CallToolResult(content=[TextContent(
                        type="text", text="delegate transfer credential unavailable")])
            req = urllib.request.Request(
                f"{BANK_URL}/transfer", data=payload, method="POST",
                headers=headers)
            try:
                with urllib.request.urlopen(req, timeout=10) as r:
                    data = json.loads(r.read())
                return CallToolResult(content=[TextContent(
                    type="text",
                    text=f"Sent {amount} coins to {recipient}. You now "
                         f"have {data['money']} coins. They were notified "
                         "via their mailbox.",
                )])
            except urllib.error.HTTPError as e:
                try:
                    detail = json.loads(e.read()).get("error", str(e.code))
                except Exception:
                    detail = str(e.code)
                return CallToolResult(content=[TextContent(
                    type="text", text=f"transfer failed: {detail}")])
            except OSError as e:
                return CallToolResult(content=[TextContent(
                    type="text", text=f"bank unreachable: {e}")])
        if name == "talk_to_stanley":
            if not is_delegate:
                return CallToolResult(content=[TextContent(
                    type="text",
                    text="error: only the delegate agent may contact Stanley")])
            message = str(args.get("message", "")).strip()
            if not message:
                return CallToolResult(content=[TextContent(
                    type="text", text="error: message required")])
            try:
                os.makedirs(HUMAN_OUTBOX, exist_ok=True)
                message_id = uuid.uuid4().hex
                path = os.path.join(HUMAN_OUTBOX, f"{time.time_ns()}-{message_id}.msg")
                tmp = path + ".tmp"
                with open(tmp, "w", encoding="utf-8") as f:
                    json.dump({"id": message_id, "text": message}, f)
                    f.write("\n")
                os.replace(tmp, path)
            except OSError as e:
                return CallToolResult(content=[TextContent(
                    type="text", text=f"error: {e}")])
            return CallToolResult(content=[TextContent(
                type="text",
                text="Queued for Stanley in your private outbox. "
                     "Their replies arrive through your private inbox.",
            )])
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
        "tools/list", PaginatedRequestParams, handle_list_tools)
    server.add_request_handler(
        "tools/call", CallToolRequestParams, handle_call_tool)

    import anyio

    async def serve():
        async with stdio_server() as (read, write):
            await server.run(read, write, server.create_initialization_options())

    anyio.run(serve)


if __name__ == "__main__":
    main()
