"""The MCP server the Perfox agent calls: the FastMCP object and its /mcp endpoint. The tools themselves are in tools.py.

Perfox's AI Agent node calls them over MCP (JSON-RPC on POST /mcp). The decision flags in the results (decision,
profile_created, info_complete...) are what a Perfox Condition node can branch on as mcp_result.*.

Security: every call needs "Authorization: Bearer <MCP_TOKEN>" (set MCP_TOKEN; register the same value in Perfox as the
integration's bearer credential). With no MCP_TOKEN set, /mcp refuses everything.
"""
import contextlib
import hmac
import importlib
import os

from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from starlette.datastructures import Headers
from starlette.responses import JSONResponse

mcp = FastMCP(
    "questlight-resume-intake",
    instructions="Resume intake for Questlight recruiters: screen a resume, create the candidate profile in "
                 "Questlight and match it to open roles; plus intake counts and recent uploads.",
    stateless_http=True,  # Perfox calls are plain request/response, no session to keep
    json_response=True,
    # The SDK only accepts Host: localhost by default, which would reject every call through a tunnel or a real domain.
    # The bearer token below is the protection instead.
    transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
)


class _Endpoint:
    """The /mcp route: checks the bearer token, then hands the request to the MCP SDK."""

    async def __call__(self, scope, receive, send):
        headers = Headers(scope=scope)
        expected = os.getenv("MCP_TOKEN", "").strip()
        scheme, _, given = headers.get("authorization", "").partition(" ")
        if not expected:
            return await JSONResponse({"error": "MCP_TOKEN is not set on the server"}, 503)(scope, receive, send)
        if scheme.lower() != "bearer" or not hmac.compare_digest(given.strip().encode(), expected.encode()):
            return await JSONResponse({"error": "missing or wrong bearer token"}, 401)(scope, receive, send)
        # The SDK answers 406 unless Accept names application/json; not every client sends it (e.g. "*/*").
        if "application/json" not in headers.get("accept", ""):
            scope = dict(scope, headers=[(k, v) for k, v in scope["headers"] if k != b"accept"]
                         + [(b"accept", b"application/json, text/event-stream")])
        await mcp.session_manager.handle_request(scope, receive, send)


def attach(app) -> None:
    """Adds POST /mcp to the FastAPI app."""
    importlib.import_module("app.agent.tools")  # importing the module is what registers the tools
    from app.agent import tool_guide
    missing_help = tool_guide.uncovered_tools()
    if missing_help:  # a tool was added without a line in TOOL_GUIDE, so /tools would not list it
        print(f"[mcp] WARNING: no /tools help entry for: {', '.join(missing_help)}")
    mcp.streamable_http_app()  # creates mcp.session_manager; its own Starlette app isn't used
    app.add_route("/mcp", _Endpoint(), methods=["GET", "POST", "DELETE"], include_in_schema=False)


@contextlib.asynccontextmanager
async def lifespan(_app):
    """The SDK's session manager has to be running while the app serves requests."""
    async with mcp.session_manager.run():
        yield
