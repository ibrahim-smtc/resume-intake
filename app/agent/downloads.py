"""Getting the file the agent points at (a resume or a job description): a file attached in the page's chat (kept on this
server) or an https link."""
import os
from pathlib import Path
from urllib.parse import unquote, urlparse

import httpx

from app import settings
from app.agent import chat_bridge
from app.intake import documents

TIMEOUT_S = 30


async def download(url: str, file_name: str):
    """Fetches the file. Returns (bytes, file name, error)."""
    if chat_bridge.is_link(url):  # a file the recruiter attached in the page's text box: it never left this server
        stored = chat_bridge.load_file(url)
        if not stored:
            return None, None, "that attached file is unknown or has expired (files are kept 30 minutes): ask the recruiter to attach it again"
        return stored[1], file_name_for(stored[0], stored[1]), None
    parts = urlparse(url or "")
    if parts.scheme != "https" or not parts.hostname:
        return None, None, "file_url must be an https link to the uploaded file"
    allowed = [h.strip().lower() for h in os.getenv("MCP_FILE_HOSTS", "").split(",") if h.strip()]
    host = parts.hostname.lower()
    if allowed and not any(host == h or host.endswith("." + h) for h in allowed):
        return None, None, f"files from {host} are not accepted (MCP_FILE_HOSTS)"
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT_S, follow_redirects=True) as client:
            async with client.stream("GET", url) as resp:
                if resp.status_code != 200:
                    return None, None, f"couldn't download the file (HTTP {resp.status_code}); the link may have expired"
                data = bytearray()
                async for chunk in resp.aiter_bytes():
                    data += chunk
                    if len(data) > settings.MAX_UPLOAD_BYTES:
                        return None, None, f"file is larger than {settings.MAX_UPLOAD_LABEL}"
    except httpx.HTTPError as exc:
        return None, None, f"couldn't download the file ({type(exc).__name__})"
    print(f"[mcp] downloaded {len(data)} bytes from {host}")
    return bytes(data), file_name_for(file_name or unquote(Path(parts.path).name), bytes(data)), None


def file_name_for(name: str, data: bytes) -> str:
    """Signed storage links often lack the extension, and the pipeline goes by it, so it is added from the content."""
    name = Path(name or "document").name
    if Path(name).suffix.lower() in chat_bridge.ACCEPTED:
        return name
    if data[:5] == b"%PDF-":
        return name + ".pdf"
    if data[:4] == b"PK\x03\x04" and b"word/document.xml" in data:
        return name + ".docx"
    if data[:8] == documents.OLE_MAGIC:
        return name + ".doc"
    if data and b"\x00" not in data[:4096]:
        try:
            data[:4096].decode("utf-8")
            return name + ".txt"
        except UnicodeDecodeError:
            pass
    return name  # unknown type: the intake check rejects it with a clear message
