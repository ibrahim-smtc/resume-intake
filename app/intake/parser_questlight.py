"""Questlight's parsing service: POST /parsing/resume reads a resume into JSON, POST /parsing/mask returns a masked PDF.

The service has no login. It is a shared dev service that hangs when it gets overlapping requests, so calls are made one
at a time.
"""
import asyncio
import base64

import httpx

from app import settings
from app.observability import tracing

# Start of the 422 MISSING_REQUIRED_FIELDS message it sends when it can't find the candidate's name, email or phone.
MISSING_MESSAGE = "We couldn't find the following details"

# DOCX goes up as octet-stream because the service hangs on a DOCX sent with its official MIME type.
MIME = {".pdf": "application/pdf", ".docx": "application/octet-stream"}
# The dev /mask API never answered for a DOCX (and then jammed the service for minutes), so only PDFs are masked.
# Add ".docx" here once Questlight fixes that.
MASKABLE = {".pdf"}

_slot = asyncio.Semaphore(1)


async def _call(client: httpx.AsyncClient, path: str, field: str, name: str, data: bytes, mime: str):
    """POST the file to a parsing endpoint. Returns (response, error message)."""
    try:
        async with _slot:
            resp = await client.post(f"{settings.PARSING_BASE_URL}{path}", files={field: (name, data, mime)})
        return resp, None
    except httpx.TimeoutException:
        return None, f"Questlight {path.strip('/')} API timed out after {settings.PARSER_TIMEOUT_S}s"
    except httpx.HTTPError as exc:
        return None, f"couldn't reach the Questlight {path.strip('/')} API ({type(exc).__name__}: {exc or 'no detail'})"


async def parse(client: httpx.AsyncClient, name: str, data: bytes, ext: str):
    """Returns (parsed resume JSON, None) or (None, error message)."""
    resp, err = await _call(client, "/resume", "pdf_doc", name, data, MIME[ext])
    if err:
        return None, err
    if resp.status_code != 200:
        try:
            return None, resp.json()["error"]["message"]
        except (ValueError, KeyError, TypeError):
            return None, f"parser returned HTTP {resp.status_code}"
    return _take_usage(resp.json()), None


def _take_usage(parsed: dict) -> dict:
    """Questlight's parser doesn't return its token counts yet (they only appear in its server logs). If it starts
    returning a "usage" block, the counts go on the trace and the block is removed from the resume data."""
    usage = parsed.pop("usage", None) if isinstance(parsed, dict) else None
    if isinstance(usage, dict):
        tracing.record_usage(usage.get("input_tokens", usage.get("prompt_tokens")),
                             usage.get("output_tokens", usage.get("completion_tokens")),
                             provider="questlight", model=usage.get("model"))
    else:
        tracing.current().set(tokens="not reported by Questlight")
    return parsed


async def mask(client: httpx.AsyncClient, name: str, data: bytes, ext: str):
    """Returns (masked PDF as base64, warning)."""
    if ext not in MASKABLE:
        return None, f"masking is PDF-only for now, so {ext} files only get the redacted JSON"
    masked, err = await _call(client, "/mask", "file", name, data, MIME[ext])
    if err:
        return None, err
    if masked.status_code == 200 and masked.content[:5] == b"%PDF-":
        return base64.b64encode(masked.content).decode("ascii"), None
    return None, f"mask API returned HTTP {masked.status_code} instead of a PDF"
