"""The page's text box -> the Perfox agent, through a Perfox Webhook trigger.

The page posts to /api/chat (app/main.py). This module forwards the message to the agent's webhook URL and hands the
agent's reply back. Perfox answers an authenticated webhook with the reply in the response body ("response_text"),
so the page just waits for it; no callback is needed.

The resume never goes to Perfox. It is kept here for 30 minutes under a random private link ("intake-file://<token>"),
and only the link goes into the message. The agent passes the link to the process_resume tool, and our own MCP server
(app/agent/downloads.py) opens it with load_file(). Nobody else can: the link only means something inside this process.

Settings: PERFOX_WEBHOOK_URL (the trigger's Full URL, from Studio) and PERFOX_WEBHOOK_SECRET (its Shared Secret).
Without both, configured() is false and the page keeps using the direct pipeline (/api/process).
"""
import os
import re
import secrets
import time
from collections import OrderedDict
from pathlib import Path

import httpx

from app import settings

SCHEME = "intake-file://"
TIMEOUT_S = 150  # the agent may call process_resume, which alone takes up to a minute
FILE_TTL_S = 30 * 60
FILE_MAX = 20  # at most 20 files held at once; the oldest goes first
_LINK_RE = re.compile(re.escape(SCHEME) + r"([0-9a-f]{24})")

_files: "OrderedDict[str, tuple]" = OrderedDict()  # token -> (file name, bytes, time stored)


def configured() -> bool:
    return bool(os.getenv("PERFOX_WEBHOOK_URL", "").strip() and os.getenv("PERFOX_WEBHOOK_SECRET", "").strip())


# ---------- the private file links ----------

def store_file(name: str, data: bytes) -> str:
    now = time.monotonic()
    for token in [t for t, (_, _, born) in _files.items() if now - born > FILE_TTL_S]:
        del _files[token]
    token = secrets.token_hex(12)
    _files[token] = (name, data, now)
    while len(_files) > FILE_MAX:
        _files.popitem(last=False)
    return SCHEME + token


def is_link(url: str) -> bool:
    return str(url or "").strip().startswith(SCHEME)


def load_file(url: str):
    """(file name, bytes) for a link made by store_file, or None when it is unknown or expired.
    Tolerates punctuation an LLM may have added around the link."""
    m = _LINK_RE.search(str(url or ""))
    entry = _files.get(m.group(1)) if m else None
    if entry and time.monotonic() - entry[2] <= FILE_TTL_S:
        return entry[0], entry[1]
    return None


def token_of(text):
    """The 24-character upload token inside an intake-file link (or on its own), else None. The agent often has only the
    attachment's link in the chat history, so it may pass the link, or just its token, where a file_id belongs."""
    match = re.search(r"(?<![0-9a-f])([0-9a-f]{24})(?![0-9a-f])", str(text or ""))
    return match.group(1) if match else None


def file_problem(name: str, data: bytes):
    if Path(name).suffix.lower() not in (".pdf", ".docx"):
        return f"{name}: I can only read PDF or DOCX files."
    if not data:
        return f"{name} is empty."
    if len(data) > settings.MAX_UPLOAD_BYTES:
        return f"{name} is larger than {settings.MAX_UPLOAD_LABEL}."
    return None


# ---------- sending ----------

def _session(session_id: str) -> str:
    """The page's random id, cleaned. It is the end-user External ID, so the agent keeps one conversation per page."""
    return re.sub(r"[^A-Za-z0-9_-]", "", str(session_id or ""))[:64] or secrets.token_hex(8)


async def send(session_id: str, message: str, file=None) -> dict:
    """file is (name, bytes) or None. Returns {"ok": True, "reply": text, "execution_id": ...} or {"ok": False, "error": ...}."""
    text = str(message or "").strip()
    if file:
        name, data = file
        problem = file_problem(name, data)
        if problem:
            return {"ok": False, "error": problem}
        link = store_file(name, data)
        text = (text + "\n\n" if text else "") + f"[Attached resume - file_name: {name}, file_url: {link}]"
    if not text:
        return {"ok": False, "error": "nothing to send"}

    body = {"session_id": _session(session_id), "message": text, "channel": "web"}
    headers = {"X-Webhook-Secret": os.getenv("PERFOX_WEBHOOK_SECRET", "").strip()}
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(TIMEOUT_S)) as client:
            resp = await client.post(os.getenv("PERFOX_WEBHOOK_URL", "").strip(), json=body, headers=headers)
    except httpx.TimeoutException:
        return {"ok": False, "error": f"the agent didn't answer within {TIMEOUT_S}s"}
    except httpx.HTTPError as exc:
        return {"ok": False, "error": f"couldn't reach Perfox ({type(exc).__name__})"}

    code = resp.status_code
    print(f"[chat] webhook -> HTTP {code}{' (with a resume)' if file else ''}")
    problems = {
        400: "Perfox refused the message (a required webhook field is missing)",
        401: "Perfox rejected the webhook secret: check PERFOX_WEBHOOK_SECRET against the trigger's Auth Secret",
        404: "Perfox can't find the agent at PERFOX_WEBHOOK_URL, or it isn't active: check the URL and activate the agent",
        405: "the webhook doesn't accept POST: set Allowed Methods to include POST on the trigger",
        422: "the agent is incomplete in Perfox (for example no AI model or personality), so it wasn't run",
    }
    if code != 200:
        return {"ok": False, "error": problems.get(code, f"Perfox returned HTTP {code}")}
    try:
        answer = resp.json()
    except ValueError:
        return {"ok": False, "error": "Perfox answered with something that isn't JSON"}
    reply = str(answer.get("response_text") or "").strip()
    if not reply:
        return {"ok": False, "error": "the agent ran but sent no reply text (execution " + str(answer.get("execution_id")) +
                "); make sure the webhook's Auth Mode is Shared Secret, since open webhooks return no reply"}
    return {"ok": True, "reply": reply, "execution_id": answer.get("execution_id")}
