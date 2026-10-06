"""Who may reach what.

/mcp (the Perfox agent's tools) is protected by its own bearer token, see app/agent/mcp_app.py. Everything else (the upload
page, the chat and upload APIs, the audit log and traces) has no login of its own, so:

- APP_PASSWORD not set (the default): the pages are open to whoever has the URL. That is fine on your machine and for a test
  deployment, but on the internet anyone with the link can use the app, create profiles in Questlight with your token, and read
  the audit log. Running on Vercel without a password prints a warning in the logs.
- APP_PASSWORD set: every request except /mcp needs HTTP Basic auth with that password (any user name). The browser asks once.
- Locally, a request that arrives through a Cloudflare tunnel (Cloudflare adds Cf-Ray and Cf-Connecting-Ip) may only reach /mcp
  when there is no password, so a development tunnel never exposes the pages.
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
        if settings.ON_VERCEL and not os.getenv("APP_PASSWORD"):
            print("[security] WARNING: APP_PASSWORD is not set, so the pages and APIs are open to anyone with this URL")

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope["path"].rstrip("/") == "/mcp":
            return await self.app(scope, receive, send)
        headers = Headers(scope=scope)
        password = os.getenv("APP_PASSWORD", "")
        if password:
            given = _basic_password(headers.get("authorization", ""))
            if not hmac.compare_digest(given.encode(), password.encode()):
                return await Response("Password required", 401, headers={"WWW-Authenticate": 'Basic realm="Resume intake"'})(scope, receive, send)
        elif not settings.ON_VERCEL and ("cf-ray" in headers or "cf-connecting-ip" in headers):
            return await JSONResponse({"detail": "Not Found"}, 404)(scope, receive, send)
        await self.app(scope, receive, send)
