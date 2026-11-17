"""Perfox structured extraction client and post-parse normalization for resumes and JDs."""
import asyncio
import json
import logging
import time
from datetime import date

import httpx

from app import net, settings
from app.intake import documents
from app.observability import llm_usage, tracing
from app.parsing.jd.fix_jd import fix_jd
from app.parsing.jd.jd_schema import build_jd_prompt, build_jd_schema
from app.parsing.resume.fix_normlization import check_required, fix_dates_and_experience, tidy_education
from app.parsing.resume.schema import build_prompt, build_schema
from app.parsing.resume.verify_identity import verify_identity

TIMEOUT_S = 90         # one extraction takes 10-20 s
MIN_TEXT_CHARS = 40    # less than this is not a readable document
JD_TEXT_LIMIT = 20000  # characters of JD text sent when the JD is not a PDF

# Start of the message that says a real resume has no name, email or phone to read. The pipeline asks the recruiter for them.
MISSING_MESSAGE = "We couldn't find the following details"

Failure = tuple[int, str, str]

log = logging.getLogger("perfox.parsing")


# ── Perfox ────────────────────────────────────────────────────────────────────
async def call_perfox(
    name: str, data: bytes, ext: str, text: str, schema: dict, prompt: str, kind: str = "resume", request_id: str | None = None
) -> tuple[dict | None, dict | None, Failure | None]:
    perfox_url = settings.PERFOX_BASE_URL
    perfox_key = settings.PERFOX_API_KEY
    if not perfox_url or not perfox_key:
        return None, None, (500, "CONFIG_ERROR", "Perfox API credentials are not configured")

    key = perfox_key[7:].strip() if perfox_key.lower().startswith("bearer ") else perfox_key

    if ext == ".pdf":
        upload_data = data
        upload_name = name
        mime = "application/pdf"
    else:
        upload_data = text.encode("utf-8")
        upload_name = name if name.endswith(".txt") else name + ".txt"
        mime = "text/plain"

    payload_data = {"schema": json.dumps(schema), "prompt": prompt}
    files = {"file": (upload_name, upload_data, mime)}
    headers = {"Authorization": f"Bearer {key}"}

    started = time.perf_counter()
    try:
        async with net.client(TIMEOUT_S) as client:
            resp = await client.post(
                f"{perfox_url}/documents/extract_structured",
                headers=headers,
                files=files,
                data=payload_data,
            )
    except httpx.TimeoutException as exc:
        _record_call(kind, request_id, _ms(started), "timeout", error=exc)
        return None, None, (504, "GATEWAY_TIMEOUT", f"Perfox timed out after {TIMEOUT_S}s")
    except httpx.HTTPError as exc:
        _record_call(kind, request_id, _ms(started), "connection" if isinstance(exc, httpx.TransportError) else "error", error=exc)
        return None, None, (502, "BAD_GATEWAY", f"Could not reach Perfox: {exc}")
    except asyncio.CancelledError as exc:
        _record_call(kind, request_id, _ms(started), "cancelled", error=exc)
        raise
    latency_ms = _ms(started)

    if resp.status_code != 200:
        _record_call(kind, request_id, latency_ms, "rate_limited" if resp.status_code == 429 else "error", http_status=resp.status_code)
        try:
            body = resp.json()
            detail = f"{body.get('error', 'Error')}: {body.get('message', resp.text)}"
        except Exception:
            detail = resp.text or f"HTTP {resp.status_code}"
        # cut short: a gateway's HTML error page would otherwise land whole in the audit log and in the agent's context
        return None, None, (502, "PERFOX_REJECTED", f"Perfox rejected the request: {detail[:300]}")

    try:
        result = resp.json()
        parsed, usage = result["data"], result.get("usage")
    except (KeyError, ValueError, TypeError, AttributeError) as exc:
        _record_call(kind, request_id, latency_ms, "error", error=exc, http_status=resp.status_code)
        return None, None, (502, "BAD_GATEWAY", "Perfox returned an invalid response structure")
    _record_call(kind, request_id, latency_ms, "ok", http_status=resp.status_code, usage=usage)
    return parsed, usage, None


def _ms(started: float) -> int:
    return int((time.perf_counter() - started) * 1000)


def _record_call(kind: str, request_id: str | None, latency_ms: int, status: str, *, error: BaseException | None = None,
                 http_status: int | None = None, usage=None) -> None:
    tokens = token_counts(usage)
    served = usage.get("model") if isinstance(usage, dict) and isinstance(usage.get("model"), str) else None
    model = settings.PERFOX_EXTRACT_MODEL or served
    tokens_in = tokens["input_tokens"] if tokens else None
    tokens_out = tokens["output_tokens"] if tokens else None
    tracing.record_usage(tokens_in, tokens_out, provider="perfox", model=model, status=status)
    if status == "ok" and not tokens:
        tracing.current().set(tokens="not reported by Perfox")
    error_type = type(error).__name__ if error else ("HTTPStatusError" if http_status and http_status != 200 else None)
    llm_usage.record(operation=llm_usage.operation_name(kind), status=status, latency_ms=latency_ms, http_status=http_status,
                     error_type=error_type, prompt_tokens=tokens_in, completion_tokens=tokens_out, model=model,
                     model_served=served, request_id=request_id or tracing.current_trace_id())


def token_counts(usage: dict | None) -> dict | None:
    if not usage:
        return None
    try:
        tokens_in, tokens_out = int(usage["input_tokens"]), int(usage["output_tokens"])
    except (TypeError, KeyError, ValueError):
        return None
    return {"input_tokens": tokens_in, "output_tokens": tokens_out, "total_tokens": tokens_in + tokens_out}


def _readable_text(data: bytes, ext: str, scanned: str) -> tuple[str | None, Failure | None]:
    """The file's text, or why it can't be used. `scanned` is the message for a file that is only pictures."""
    text, has_images, doc_err = documents.extract_text(data, ext)
    if doc_err:
        return None, (422, "UNREADABLE_DOCUMENT", doc_err)
    clean = text.strip()
    if len(clean) < MIN_TEXT_CHARS:
        return None, (422, "UNREADABLE_DOCUMENT", scanned if has_images else "Not enough readable text in this file.")
    return clean, None


# ── Reading a resume or a JD ──────────────────────────────────────────────────
async def read_resume(filename: str, ext: str, data: bytes, request_id: str | None = None) -> tuple[dict | None, Failure | None]:
    """The resume as JSON (with a "usage" key: the tokens Perfox reports), or a failure."""
    clean, failure = _readable_text(data, ext, "This looks like a scanned image — no text layer found.")
    if failure:
        return None, failure
    log.info("resume ext=%s size=%d text_chars=%d", ext, len(data), len(clean))

    parsed, usage, failure = await call_perfox(filename, data, ext, clean, build_schema(), build_prompt(date.today()),
                                               kind="resume", request_id=request_id)
    if failure:
        return None, failure

    parsed, corrected = verify_identity(parsed, clean)
    if corrected:
        log.info("identity corrected: %s", ", ".join(corrected))

    parsed = fix_dates_and_experience(parsed, date.today())
    parsed = tidy_education(parsed)

    parsed, missing = check_required(parsed, clean)
    if missing:
        items = ", ".join(missing)
        msg = f"{MISSING_MESSAGE} in this resume: {items}. Please make sure it clearly shows the name, email, and phone number."
        return None, (422, "MISSING_REQUIRED_FIELDS", msg)

    body = dict(parsed)
    body["usage"] = token_counts(usage)
    return body, None


async def read_jd(filename: str, ext: str, data: bytes, request_id: str | None = None) -> tuple[dict | None, Failure | None]:
    """The job description as JSON (with a "usage" key), or a failure."""
    clean, failure = _readable_text(data, ext, "This looks like a scanned image - no text layer found")
    if failure:
        return None, failure
    if ext != ".pdf":
        clean = clean[:JD_TEXT_LIMIT]
    log.info("jd ext=%s size=%d text_chars=%d", ext, len(data), len(clean))

    parsed, usage, failure = await call_perfox(filename, data, ext, clean, build_jd_schema(), build_jd_prompt(date.today()),
                                               kind="jd", request_id=request_id)
    if failure:
        return None, failure

    body = dict(fix_jd(parsed, date.today()))
    body["usage"] = token_counts(usage)
    return body, None


# ── What the pipeline calls ───────────────────────────────────────────────────
async def parse_resume(name: str, data: bytes, ext: str):
    """Returns (parsed resume JSON, None) or (None, error message). Records Perfox's token counts on the current trace step."""
    body, failure = await read_resume(name, ext, data)
    if failure:
        return None, failure[2]
    body.pop("usage", None)
    return body, None


async def parse_jd(name: str, data: bytes, ext: str, text: str):
    """Returns (parsed JD JSON, None) or (None, error message). A PDF goes up as the file; anything else (DOCX, TXT, old .doc,
    pasted text) as the text the caller already read out of it."""
    if ext == ".pdf":
        body, failure = await read_jd(name, ext, data)
    else:
        body, failure = await read_jd("jd.txt", ".txt", text[:JD_TEXT_LIMIT].encode("utf-8"))
    if failure:
        return None, failure[2]
    body.pop("usage", None)
    return body, None
