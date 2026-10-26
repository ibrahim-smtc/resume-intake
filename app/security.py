"""Who may reach what.

/mcp (the Perfox agent's tools) is protected by its own bearer token, see app/agent/mcp_app.py.
/parsing/health is always public.
/parsing/* endpoints accept PERFOX_PARSERS_TOKEN (Bearer or X-Service-Token) or APP_PASSWORD (HTTP Basic).
Everything else (the upload page, the chat and upload APIs, the audit log and traces) has no login of its own, so:

- APP_PASSWORD not set (the default): the pages are open to whoever has the URL. That is fine on your machine and for a test
  deployment, but on the internet anyone with the link can use the app, create profiles in Questlight with your token, and read
  the audit log. Running on Vercel without a password prints a warning in the logs.
- APP_PASSWORD set: every request except /mcp (and token-authorized /parsing) needs HTTP Basic auth with that password.
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
        if settings.ON_VERCEL:
            password = os.getenv("APP_PASSWORD", "") or getattr(settings, "APP_PASSWORD", "")
            token = os.getenv("PERFOX_PARSERS_TOKEN", "") or getattr(settings, "PERFOX_PARSERS_TOKEN", "")
            if not password:
                print("[security] WARNING: APP_PASSWORD is not set, so the pages and APIs are open to anyone with this URL")
            if not token and not password:
                print("[security] WARNING: neither PERFOX_PARSERS_TOKEN nor APP_PASSWORD is set, so /parsing endpoints are open to anyone")

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)

        path = scope["path"].rstrip("/")
        if path == "/mcp" or path == "/parsing/health":
            return await self.app(scope, receive, send)

        headers = Headers(scope=scope)
        token = os.getenv("PERFOX_PARSERS_TOKEN", "") or getattr(settings, "PERFOX_PARSERS_TOKEN", "")
        password = os.getenv("APP_PASSWORD", "") or getattr(settings, "APP_PASSWORD", "")

        auth_header = headers.get("authorization", "")
        service_token = headers.get("x-service-token", "")

        # Check if the request provided a valid parsing token (Bearer or X-Service-Token)
        token_valid = False
        if token:
            if auth_header.lower().startswith("bearer "):
                provided_bearer = auth_header[7:].strip()
                if hmac.compare_digest(provided_bearer.encode("utf-8"), token.encode("utf-8")):
                    token_valid = True
            if service_token and hmac.compare_digest(service_token.strip().encode("utf-8"), token.encode("utf-8")):
                token_valid = True

        # Check if the request provided a valid Basic password
        given_pwd = _basic_password(auth_header)
        password_valid = bool(password and hmac.compare_digest(given_pwd.encode("utf-8"), password.encode("utf-8")))

        # ── 1. /parsing/* endpoints ───────────────────────────────────────────
        if path.startswith("/parsing"):
            if not token and not password:
                return await self.app(scope, receive, send)
            if token_valid or password_valid:
                return await self.app(scope, receive, send)
            if password and not (auth_header.lower().startswith("bearer ") or service_token):
                return await Response(
                    "Password required", 401, headers={"WWW-Authenticate": 'Basic realm="Resume intake"'}
                )(scope, receive, send)
            return await JSONResponse(
                {"success": False, "error": {"code": 401, "error_type": "UNAUTHORIZED", "message": "Valid bearer token or password required"}},
                401,
            )(scope, receive, send)

        # ── 2. All other paths (pages, /api/..., etc.) ─────────────────────────
        # Note: parsing token must NOT unlock pages or other APIs
        if password:
            if not password_valid:
                return await Response(
                    "Password required", 401, headers={"WWW-Authenticate": 'Basic realm="Resume intake"'}
                )(scope, receive, send)
        elif not settings.ON_VERCEL and ("cf-ray" in headers or "cf-connecting-ip" in headers):
            return await JSONResponse({"detail": "Not Found"}, 404)(scope, receive, send)

        await self.app(scope, receive, send)
