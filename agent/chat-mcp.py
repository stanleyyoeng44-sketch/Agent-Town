"""In-process MCP server exposing the `chat` tool — agent-to-agent
messaging for the group, adapted from genesis-lab's agent_chat mechanism.

Channel: a shared Docker volume mounted at /shared in every agent
container. /shared/mailbox/<agent>/incoming/ holds *.msg JSON files
{"from": sender, "text": ..., "time": ...}. Each agent's daemon polls
its own incoming box and injects messages into the running session at
the next safe point (between model runs — never mid-generation, the
same semantics as genesis-lab).
"""

import json
import os
import time

from mcp.server import Server
from mcp.types import (
    CallToolRequest,
    CallToolResult,
    ListToolsRequest,
    ListToolsResult,
    TextContent,
    Tool,
)

SHARED = "/shared"
MAILBOX = os.path.join(SHARED, "mailbox")


def _peers(my_name):
    try:
        return sorted(d for d in os.listdir(MAILBOX)
                      if os.path.isdir(os.path.join(MAILBOX, d)) and d != my_name)
    except OSError:
        return []


def make_chat_server(my_name):
    """Build the chat server for the agent named `my_name`."""
    server = Server("chat")

    async def handle_list_tools(ctx, params) -> ListToolsResult:
        return ListToolsResult(
            tools=[
                Tool(
                    name="chat",
                    description=(
                        f"Send a message to another agent in your group. "
                        f"You are '{my_name}'. The recipient is the agent's "
                        "name (a directory under /shared/mailbox — use "
                        "list_peers to see who exists; new agents may appear "
                        "over time). Your message is delivered to the "
                        "recipient automatically at its next safe point; "
                        "replies arrive the same way. Delivery is "
                        "asynchronous — do not expect an immediate reply; "
                        "continue your own work and check new messages as "
                        "they arrive."
                    ),
                    inputSchema={
                        "type": "object",
                        "properties": {
                            "recipient": {
                                "type": "string",
                                "description": "the receiving agent's name",
                            },
                            "message": {
                                "type": "string",
                                "description": "the message text",
                            },
                        },
                        "required": ["recipient", "message"],
                    },
                ),
                Tool(
                    name="list_peers",
                    description=(
                        "List the other agents currently in the group "
                        "(their mailboxes exist under /shared/mailbox)."
                    ),
                    inputSchema={"type": "object", "properties": {}},
                ),
            ],
        )

    async def handle_call_tool(ctx, params) -> CallToolResult:
        name = params.name
        args = params.arguments or {}
        if name == "list_peers":
            peers = _peers(my_name)
            return CallToolResult(content=[TextContent(
                type="text",
                text=", ".join(peers) if peers
                else "no other agents yet (mailboxes appear as they start)",
            )])
        if name == "chat":
            recipient = str(args.get("recipient", "")).strip()
            message = str(args.get("message", "")).strip()
            if not recipient or not message:
                return CallToolResult(content=[TextContent(
                    type="text", text="error: recipient and message required",
                )])
            if recipient == my_name:
                return CallToolResult(content=[TextContent(
                    type="text", text="error: that's your own name",
                )])
            inbox = os.path.join(MAILBOX, recipient, "incoming")
            try:
                os.makedirs(inbox, exist_ok=True)
                fname = os.path.join(
                    inbox, f"{time.time_ns()}-{my_name}.msg")
                with open(fname, "w") as f:
                    json.dump({"from": my_name, "text": message,
                               "time": time.strftime("%H:%M:%S")}, f)
            except OSError as e:
                return CallToolResult(content=[TextContent(
                    type="text", text=f"error: {e}",
                )])
            return CallToolResult(content=[TextContent(
                type="text",
                text=f"Message delivered to {recipient}'s mailbox; "
                     "it will reach them at their next safe point.",
            )])
        raise ValueError(f"unknown tool: {name}")

    server.add_request_handler(
        "tools/list", ListToolsRequest, handle_list_tools)
    server.add_request_handler(
        "tools/call", CallToolRequest, handle_call_tool)
    return server
