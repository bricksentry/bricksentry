"""MCP server exposing bricksentry's agent tools.

Run:  ``bricksentry mcp``  (stdio)  or  ``bricksentry mcp --transport streamable-http``

Over HTTP the server listens on 127.0.0.1:8000 by default. On Databricks Apps it reads
``DATABRICKS_APP_PORT`` and listens on 0.0.0.0, so ``bricksentry mcp --transport
streamable-http`` is all an ``app.yaml`` needs (see deploy/databricks-app). ``--host`` /
``--port`` or ``BRICKSENTRY_HOST`` / ``BRICKSENTRY_PORT`` override this. The endpoint is ``/mcp``.

Claude Code:  ``claude mcp add bricksentry -- uvx bricksentry-mcp --config /full/path/bricksentry.yml``
"""

from __future__ import annotations

import os
import sys

from bricksentry import agent_tools

INSTRUCTIONS = """bricksentry checks whether a change to Databricks data is safe.
Recommended loop when you change a pipeline or table:
1. whats_downstream(table) to learn the blast radius before editing.
2. rollback_plan(table) and keep the SQL.
3. preview_write(sql, target) for any direct write, or assess_change(tables) after
   your change has built candidate tables.
4. safe_to_ship(assessment_id) and follow `next_step` exactly.
Never bypass a needs_human or block verdict."""

READ_ONLY_NOTE = """
This server is read-only: preview_write is not available. Build candidate tables
in your pipeline and use assess_change instead."""


_LOCALHOST = ("127.0.0.1", "localhost", "::1")


def http_address(host: str | None = None, port: int | str | None = None) -> tuple[str, int]:
    """Where the HTTP transports listen: flags, then BRICKSENTRY_HOST/PORT, then
    Databricks Apps (DATABRICKS_APP_PORT on 0.0.0.0), then 127.0.0.1:8000."""
    app_port = os.environ.get("DATABRICKS_APP_PORT")
    port = port or os.environ.get("BRICKSENTRY_PORT") or app_port or 8000
    host = host or os.environ.get("BRICKSENTRY_HOST") or ("0.0.0.0" if app_port else "127.0.0.1")
    return host, int(port)


def tools(read_only: bool = False) -> list:
    return [t for t in agent_tools.TOOLS if not read_only or t.__name__ in agent_tools.READ_ONLY]


def build_server(read_only: bool = False):
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

    from bricksentry import __version__

    instructions = INSTRUCTIONS + (READ_ONLY_NOTE if read_only else "")
    try:  # mcp >= 2 takes the version directly
        server = Server(name="bricksentry", instructions=instructions, version=__version__)
    except TypeError:  # mcp 1.x: set it on the underlying low-level server
        server = Server(name="bricksentry", instructions=instructions)
        low = getattr(server, "_mcp_server", None)
        if low is not None and hasattr(low, "version"):
            low.version = __version__
    for fn in tools(read_only):
        ann = None
        if ToolAnnotations is not None:
            ro = fn.__name__ in agent_tools.READ_ONLY
            ann = ToolAnnotations(readOnlyHint=ro, destructiveHint=False, idempotentHint=ro)
        server.tool(name=fn.__name__, description=fn.__doc__, annotations=ann)(fn)
    return server


def serve(
    transport: str = "stdio",
    host: str | None = None,
    port: int | None = None,
    read_only: bool = False,
) -> None:
    server = build_server(read_only=read_only)
    if transport == "stdio":
        server.run(transport="stdio")
        return
    host, port = http_address(host, port)
    path = "/mcp" if transport == "streamable-http" else "/sse"
    mode = ", read-only" if read_only else ""
    print(f"bricksentry MCP server ({transport}{mode}) on http://{host}:{port}{path}", file=sys.stderr, flush=True)
    kwargs = {"host": host, "port": port}
    if transport == "streamable-http":
        kwargs["stateless_http"] = True  # no session affinity needed behind a load balancer (Databricks Apps)
    try:  # mcp >= 2: transport options are run() arguments
        server.run(transport=transport, **kwargs)
    except TypeError:  # mcp 1.x: they are server settings
        for k, v in kwargs.items():
            setattr(server.settings, k, v)
        if host not in _LOCALHOST and hasattr(server.settings, "transport_security"):
            # mcp 1.x set localhost-only DNS-rebinding protection when the server was created
            # (host defaulted to 127.0.0.1). Behind a proxy such as Databricks Apps the Host header
            # is the app's domain, so keep mcp 2's behaviour: protection for localhost only.
            server.settings.transport_security = None
        server.run(transport=transport)
