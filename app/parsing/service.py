"""The app's one parser: Perfox's extract_structured reads resumes and job descriptions here.

The pipeline (app/pipeline.py for resumes, app/job_pipeline.py for JDs) calls parse_resume and parse_jd directly, in the same
process. The /parsing routes (router.py) call read_resume and read_jd and turn a failure into an HTTP response. Both do the same
work, so a resume or JD is read the same way whichever door it came through.

A failure is (HTTP status, error code, message). parse_resume and parse_jd keep only the message.
"""
import json
import logging
from datetime import date

import httpx

from app import settings
from app.observability import tracing
from app.parsing import documents
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
    name: str, data: bytes, ext: str, text: str, schema: dict, prompt: str
) -> tuple[dict | None, dict | None, Failure | None]:
    perfox_url = settings.PERFOX_BASE_URL
    perfox_key = settings.PERFOX_API_KEY
    if not perfox_url or not perfox_key:
        return None, None, (500, "CONFIG_ERROR", "Perfox API credentials are not configured")

    key = perfox_key[7:] if perfox_key.lower().startswith("bearer") else perfox_key

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

    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(TIMEOUT_S)) as client:
            resp = await client.post(
                f"{perfox_url}/documents/extract_structured",
                headers=headers,
                files=files,
                data=payload_data,
            )
    except httpx.TimeoutException:
        return None, None, (504, "GATEWAY_TIMEOUT", f"Perfox timed out after {TIMEOUT_S}s")
    except httpx.HTTPError as exc:
        return None, None, (502, "BAD_GATEWAY", f"Could not reach Perfox: {exc}")

    if resp.status_code != 200:
        try:
            body = resp.json()
            detail = f"{body.get('error', 'Error')}: {body.get('message', resp.text)}"
        except Exception:
            detail = resp.text or f"HTTP {resp.status_code}"
        return None, None, (502, "PERFOX_REJECTED", f"Perfox rejected the request: {detail}")

    try:
        result = resp.json()
        return result["data"], result.get("usage"), None
    except (KeyError, ValueError, TypeError, AttributeError):
        return None, None, (502, "BAD_GATEWAY", "Perfox returned an invalid response structure")


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
async def read_resume(filename: str, ext: str, data: bytes) -> tuple[dict | None, Failure | None]:
    """The resume as JSON (with a "usage" key: the tokens Perfox reports), or a failure."""
    clean, failure = _readable_text(data, ext, "This looks like a scanned image — no text layer found.")
    if failure:
        return None, failure
    log.info("resume ext=%s size=%d text_chars=%d", ext, len(data), len(clean))

    parsed, usage, failure = await call_perfox(filename, data, ext, clean, build_schema(), build_prompt(date.today()))
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


async def read_jd(filename: str, ext: str, data: bytes) -> tuple[dict | None, Failure | None]:
    """The job description as JSON (with a "usage" key), or a failure."""
    clean, failure = _readable_text(data, ext, "This looks like a scanned image - no text layer found")
    if failure:
        return None, failure
    if ext != ".pdf":
        clean = clean[:JD_TEXT_LIMIT]
    log.info("jd ext=%s size=%d text_chars=%d", ext, len(data), len(clean))

    parsed, usage, failure = await call_perfox(filename, data, ext, clean, build_jd_schema(), build_jd_prompt(date.today()))
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
    usage = body.pop("usage", None)
    if usage:
        tracing.record_usage(usage["input_tokens"], usage["output_tokens"], provider="perfox")
    else:
        tracing.current().set(tokens="not reported by Perfox")
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
    return body, None
