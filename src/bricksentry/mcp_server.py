"""MCP server exposing bricksentry's agent tools.

Run:  ``bricksentry mcp``  (stdio)  or  ``bricksentry mcp --transport streamable-http``

Over HTTP the server listens on 127.0.0.1:8000 by default. On Databricks Apps it reads
``DATABRICKS_APP_PORT`` and listens on 0.0.0.0, so ``bricksentry mcp --transport
streamable-http`` is all an ``app.yaml`` needs (see deploy/databricks-app). ``--host`` /
``--port`` or ``BRICKSENTRY_HOST`` / ``BRICKSENTRY_PORT`` override this. The endpoint is ``/mcp``.

Listening on anything but localhost needs ``BRICKSENTRY_MCP_TOKEN`` (clients send
``Authorization: Bearer <token>``), except on Databricks Apps, whose sign-in is in front.

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


class AuthError(RuntimeError):
    pass


def _token() -> str | None:
    return os.environ.get("BRICKSENTRY_MCP_TOKEN") or None


def check_exposure(host: str, insecure_no_auth: bool = False) -> str | None:
    """Refuse to serve tools to the network without auth. Returns the token to require
    (or None). Localhost and Databricks Apps (which puts its own sign-in in front) may run
    without one; any other address needs BRICKSENTRY_MCP_TOKEN, or an explicit
    --insecure-no-auth for a network you control."""
    token = _token()
    if token or host in _LOCALHOST or os.environ.get("DATABRICKS_APP_PORT") or insecure_no_auth:
        return token
    raise AuthError(
        f"Refusing to serve bricksentry's tools on {host} without authentication: anyone who can reach "
        "the port could run them. Set BRICKSENTRY_MCP_TOKEN (clients send it as 'Authorization: Bearer "
        "<token>'), listen on 127.0.0.1, or pass --insecure-no-auth if a proxy you control already "
        "authenticates every request."
    )


class BearerAuth:
    """ASGI middleware: every HTTP request needs ``Authorization: Bearer <token>``."""

    def __init__(self, app, token: str):
        self.app = app
        self._token = token.encode()

    async def __call__(self, scope, receive, send):
        if scope.get("type") == "http":
            import hmac

            got = b""
            for k, v in scope.get("headers") or []:
                if k.lower() == b"authorization" and v[:7].lower() == b"bearer ":
                    got = v[7:].strip()
            if not hmac.compare_digest(got, self._token):
                body = b'{"error": "unauthorized: send Authorization: Bearer <BRICKSENTRY_MCP_TOKEN>"}'
                await send({"type": "http.response.start", "status": 401,
                            "headers": [(b"content-type", b"application/json"),
                                        (b"www-authenticate", b"Bearer")]})
                await send({"type": "http.response.body", "body": body})
                return
        await self.app(scope, receive, send)


def http_app(transport: str, host: str, port: int, read_only: bool = False, token: str | None = None):
    """The ASGI app for an HTTP transport, with DNS-rebinding protection on localhost
    and bearer auth when a token is set."""
    server = build_server(read_only=read_only)
    security = None
    try:
        from mcp.server.transport_security import TransportSecuritySettings
    except ImportError:  # pragma: no cover
        TransportSecuritySettings = None
    if TransportSecuritySettings is not None:
        allowed = [h.strip() for h in os.environ.get("BRICKSENTRY_ALLOWED_HOSTS", "").split(",") if h.strip()]
        if host in _LOCALHOST:
            allowed += [f"{h}:{port}" for h in ("127.0.0.1", "localhost", "[::1]")] + ["127.0.0.1", "localhost"]
        if allowed:  # a browser page on another site can't reach a localhost or listed server via DNS tricks
            security = TransportSecuritySettings(enable_dns_rebinding_protection=True, allowed_hosts=allowed,
                                                 allowed_origins=[f"http://{h}" for h in allowed])
        else:  # behind a proxy (Databricks Apps) the Host header is the proxy's: the token protects instead
            security = TransportSecuritySettings(enable_dns_rebinding_protection=False)
    if transport == "streamable-http":
        try:  # mcp >= 2
            app = server.streamable_http_app(stateless_http=True, transport_security=security, host=host)
        except TypeError:  # mcp 1.x: options are settings
            server.settings.stateless_http = True
            server.settings.host, server.settings.port = host, port
            if hasattr(server.settings, "transport_security"):
                server.settings.transport_security = security
            app = server.streamable_http_app()
    else:
        try:
            app = server.sse_app(transport_security=security, host=host)
        except TypeError:
            server.settings.host, server.settings.port = host, port
            if hasattr(server.settings, "transport_security"):
                server.settings.transport_security = security
            app = server.sse_app()
    return BearerAuth(app, token) if token else app


def serve(
    transport: str = "stdio",
    host: str | None = None,
    port: int | None = None,
    read_only: bool = False,
    insecure_no_auth: bool = False,
) -> None:
    if transport == "stdio":
        build_server(read_only=read_only).run(transport="stdio")
        return
    host, port = http_address(host, port)
    token = check_exposure(host, insecure_no_auth)
    app = http_app(transport, host, port, read_only=read_only, token=token)
    path = "/mcp" if transport == "streamable-http" else "/sse"
    mode = ", read-only" if read_only else ""
    auth = ", bearer token required" if token else (", NO AUTH" if host not in _LOCALHOST else "")
    print(f"bricksentry MCP server ({transport}{mode}{auth}) on http://{host}:{port}{path}", file=sys.stderr, flush=True)
    import uvicorn

    uvicorn.run(app, host=host, port=port, log_level="warning")
