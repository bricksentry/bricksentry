"""MCP server exposing bricksentry's agent tools.

Run:  ``bricksentry mcp``  (stdio)  or  ``bricksentry mcp --transport streamable-http``

Claude Code:  ``claude mcp add bricksentry -- bricksentry mcp --config /path/bricksentry.yml``
"""

from __future__ import annotations

from bricksentry import agent_tools

INSTRUCTIONS = """bricksentry checks whether a change to Databricks data is safe.
Recommended loop when you change a pipeline or table:
1. whats_downstream(table) to learn the blast radius before editing.
2. rollback_plan(table) and keep the SQL.
3. preview_write(sql, target) for any direct write, or assess_change(tables) after
   your change has built candidate tables.
4. safe_to_ship(assessment_id) and follow `next_step` exactly.
Never bypass a needs_human or block verdict."""


def build_server():
    try:  # mcp >= 2
        from mcp.server.mcpserver import MCPServer as Server
    except ImportError:
        try:  # mcp 1.x
            from mcp.server.fastmcp import FastMCP as Server
        except ImportError as e:  # pragma: no cover
            raise ImportError("The MCP server needs `pip install bricksentry[mcp]`") from e

    try:
        from mcp.types import ToolAnnotations
    except ImportError:  # pragma: no cover
        ToolAnnotations = None

    server = Server(name="bricksentry", instructions=INSTRUCTIONS)
    for fn in agent_tools.TOOLS:
        ann = None
        if ToolAnnotations is not None:
            ro = fn.__name__ in agent_tools.READ_ONLY
            ann = ToolAnnotations(readOnlyHint=ro, destructiveHint=False, idempotentHint=ro)
        server.tool(name=fn.__name__, description=fn.__doc__, annotations=ann)(fn)
    return server


def serve(transport: str = "stdio") -> None:
    build_server().run(transport=transport)
