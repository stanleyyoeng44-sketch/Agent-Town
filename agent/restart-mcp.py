"""In-process MCP server exposing the single `restart` tool.

Attached via ClaudeAgentOptions(mcp_servers={"life":
{"type": "sdk", "instance": <mcp.server.Server>}}) — the SDK bridges it
to the CLI over the control channel, no subprocess involved.

Uses the mcp 2.3.0 request-handler API (the old @server.list_tools()
decorator sugar was removed): handlers are registered with
add_request_handler(method, RequestType, handler), and each handler
receives the request object.

When the agent calls `restart`, the handler flips a flag the daemon gave
it. The daemon's message loop sees the flag, ends the session, and a new
life begins with a clean context. The tool itself clears nothing — it is
only the agent's way of saying "I'm settled, let me sleep."
"""

from mcp.server import Server
from mcp.types import (
    CallToolRequest,
    CallToolResult,
    ListToolsRequest,
    ListToolsResult,
    PaginatedResult,
    TextContent,
    Tool,
)


def make_restart_server() -> tuple[Server, callable]:
    """Build the server and a restart-flag accessor.

    Returns (server, was_restart_requested). The daemon polls the flag in
    its message loop; once the tool fires, the flag stays True forever
    (a life can only sleep once).
    """
    state = {"requested": False}
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
                        "diary at /workspace/self/diary.md, and write your "
                        "wake-up prompt to /workspace/self/wake.md "
                        "(instructions from your past self to your next "
                        "session), then call this. Everything not saved "
                        "in files is lost when you sleep."
                    ),
                    inputSchema={
                        "type": "object",
                        "properties": {},
                    },
                )
            ],
        )

    async def handle_call_tool(ctx, params) -> CallToolResult:
        if params.name != "restart":
            raise ValueError(
                f"unknown tool: {params.name}")
        state["requested"] = True
        return CallToolResult(
            content=[TextContent(
                type="text",
                text=(
                    "Restart requested. The session ends now. Your next "
                    "session begins fresh, opened with the wake-up prompt "
                    "you wrote to /workspace/self/wake.md."
                ),
            )],
        )

    server.add_request_handler(
        "tools/list", ListToolsRequest, handle_list_tools)
    server.add_request_handler(
        "tools/call", CallToolRequest, handle_call_tool)

    return server, lambda: state["requested"]
