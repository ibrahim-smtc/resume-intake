"""Who may reach what.

/mcp (the Perfox agent's tools) is protected by its own bearer token, see app/agent/mcp_app.py. Everything else (the
upload page, the chat and upload APIs, the audit log and traces) has no login of its own and can create profiles in
Questlight, so this guard decides:

- APP_PASSWORD set: every request except /mcp needs HTTP Basic auth with that password (any user name). Use this when
  the app is reachable from the internet, e.g. on Vercel. The browser asks once and remembers.
- APP_PASSWORD not set, running on Vercel: everything except /mcp answers 503. The app never serves these pages to the
  open internet by accident.
- APP_PASSWORD not set, running locally: open on this machine. A request that arrives through a Cloudflare tunnel
  (Cloudflare adds Cf-Ray and Cf-Connecting-Ip) may only reach /mcp, so a tunnel never exposes the pages.
"""
import base64
import hmac
import os

from starlette.datastructures import Headers
from starlette.responses import JSONResponse, Response

from app import settings


def _basic_password(header: str) -> str:
    scheme, _, value = header.partition(" ")
    if scheme.lower() != "basic":
        return ""
    try:
        return base64.b64decode(value.strip()).decode("utf-8").partition(":")[2]
    except (ValueError, UnicodeDecodeError):
        return ""


class AccessGuard:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope["path"].rstrip("/") == "/mcp":
            return await self.app(scope, receive, send)
        headers = Headers(scope=scope)
        password = os.getenv("APP_PASSWORD", "")
        if password:
            given = _basic_password(headers.get("authorization", ""))
            if not hmac.compare_digest(given.encode(), password.encode()):
                return await Response("Password required", 401, headers={"WWW-Authenticate": 'Basic realm="Resume intake"'})(scope, receive, send)
        elif settings.ON_VERCEL:
            return await JSONResponse({"detail": "Set APP_PASSWORD to open this app on Vercel"}, 503)(scope, receive, send)
        elif "cf-ray" in headers or "cf-connecting-ip" in headers:
            return await JSONResponse({"detail": "Not Found"}, 404)(scope, receive, send)
        await self.app(scope, receive, send)
