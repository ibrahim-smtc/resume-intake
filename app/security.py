"""HTTP access control middleware.

Excludes /mcp (bearer token), /parsing/*, and tokenized /fill/* form routes from APP_PASSWORD.
With APP_PASSWORD unset, the pages and APIs are open.
"""
import base64
import hmac
import os

from starlette.datastructures import Headers
from starlette.responses import Response

from app import settings

FORM_PATHS = ("/fill/", "/api/form/")
FORM_FILES = {"/static/form-view.js", "/static/form.css"}


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
        path = scope.get("path", "")
        if (scope["type"] != "http" or path.rstrip("/") == "/mcp" or path == "/parsing" or path.startswith("/parsing/")
                or path.startswith(FORM_PATHS) or path in FORM_FILES):
            return await self.app(scope, receive, send)
        headers = Headers(scope=scope)
        password = os.getenv("APP_PASSWORD", "")
        if password:
            given = _basic_password(headers.get("authorization", ""))
            if not hmac.compare_digest(given.encode(), password.encode()):
                return await Response("Password required", 401, headers={"WWW-Authenticate": 'Basic realm="Resume intake"'})(scope, receive, send)
        await self.app(scope, receive, send)
