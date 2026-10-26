"""Parsing routes: POST /parsing/resume, /parsing/jd, /parsing/mask
Moved from the separate perfox-parsers Vercel deployment into the main app.
"""
# ── Standard Library ──────────────────────────────────────────────────────────
import asyncio
import json
import logging
import os
import re
import sys
import time
import uuid
from datetime import date
from pathlib import Path
from urllib.parse import quote

# ── Third-Party ───────────────────────────────────────────────────────────────
import httpx
from fastapi import APIRouter, File, Form, UploadFile
from fastapi.responses import JSONResponse, Response

# ── Local Modules (same package) ─────────────────────────────────────────────
from app.parsing import documents
from app.parsing.fix_jd import fix_jd
from app.parsing.fix_normlization import check_required, fix_dates_and_experience, tidy_education
from app.parsing.jd_schema import build_jd_prompt, build_jd_schema
from app.parsing.schema import build_prompt, build_schema
from app.parsing.verify_identity import verify_identity

# ── 1. Configuration ──────────────────────────────────────────────────────────
PERFOX_BASE_URL: str = os.getenv("PERFOX_BASE_URL", "").rstrip("/")
PERFOX_API_KEY: str = os.getenv("PERFOX_API_KEY", "")
MAX_FILE_BYTES: int = 10 * 1024 * 1024  # 10 MB limit
ALLOWED_EXTS: set[str] = {".pdf", ".docx"}
TIMEOUT_S: int = 90
MIN_TEXT_CHARS: int = 40
TODAY: str = date.today().isoformat()

JD_ALLOWED_EXTS: set[str] = {".pdf", ".docx", ".txt"}
JD_TEXT_LIMIT: int = 20000

# ── 2. Logging ────────────────────────────────────────────────────────────────
log = logging.getLogger("perfox.parsing")

# ── 3. Router ─────────────────────────────────────────────────────────────────
router = APIRouter()


def fail(status: int, code: str, message: str, req_id: str) -> JSONResponse:
    """Single place that builds every error response."""
    log.warning("status=%s code=%s msg=%r", status, code, message)
    return JSONResponse(
        status_code=status,
        content={
            "success": False,
            "error": {
                "code": status,
                "error_type": code,
                "message": message,
                "request_id": req_id,
            },
        },
        headers={"X-Request-ID": req_id},
    )


def ok(body: dict, req_id: str) -> JSONResponse:
    return JSONResponse(content=body, headers={"X-Request-ID": req_id})


# ── 4. Intake Validation ──────────────────────────────────────────────────────
async def read_upload(pdf_doc: UploadFile, allowed: set[str] = ALLOWED_EXTS) -> tuple[str, str, bytes]:
    filename = Path(pdf_doc.filename or "").name.strip()
    if not filename:
        ext_list = [".pdf", ".docx"] if allowed == ALLOWED_EXTS else sorted(allowed)
        types = " or ".join(e.lstrip(".").upper() for e in ext_list)
        raise ValueError((400, "INVALID_FILE", f"No filename — upload a {types}."))

    ext = Path(filename).suffix.lower()
    if ext not in allowed:
        ext_list = " or ".join(sorted(allowed))
        raise ValueError((400, "INVALID_FILE", f"'{ext}' is not supported. Send a {ext_list}."))

    data = await pdf_doc.read()
    if not data:
        raise ValueError((400, "INVALID_FILE", "The file is empty (0 bytes)."))

    if len(data) > MAX_FILE_BYTES:
        mb = MAX_FILE_BYTES // (1024 * 1024)
        raise ValueError((400, "INVALID_FILE", f"File is too large (limit {mb} MB)."))

    return filename, ext, data


# ── 5. Text Extraction ────────────────────────────────────────────────────────
def get_text(data: bytes, ext: str) -> tuple[str, bool, str | None]:
    return documents.extract_text(data, ext)


# ── 6. Perfox API Client ──────────────────────────────────────────────────────
async def call_perfox(
    name: str, data: bytes, ext: str, text: str, schema: dict, prompt: str
) -> tuple[dict | None, dict | None, tuple[int, str, str] | None]:
    if not PERFOX_BASE_URL or not PERFOX_API_KEY:
        return None, None, (500, "CONFIG_ERROR", "Perfox API credentials are not configured")

    key = PERFOX_API_KEY[7:] if PERFOX_API_KEY.lower().startswith("bearer") else PERFOX_API_KEY

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
                f"{PERFOX_BASE_URL}/documents/extract_structured",
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


# ── 7. Routes ─────────────────────────────────────────────────────────────────
@router.get("/parsing/health")
def health():
    return {
        "status": "ok",
        "timestamp": time.time(),
        "date": TODAY,
        "perfox_configured": bool(PERFOX_BASE_URL and PERFOX_API_KEY),
    }


@router.post("/parsing/resume")
async def parse_resume(pdf_doc: UploadFile = File(...)):
    req_id = str(uuid.uuid4())
    t0 = time.monotonic()

    try:
        filename, ext, data = await read_upload(pdf_doc)
    except ValueError as exc:
        status, code, msg = exc.args[0]
        return fail(status, code, msg, req_id)

    log.info("upload ext=%s size=%d", ext, len(data))

    text, has_images, doc_err = get_text(data, ext)
    if doc_err:
        return fail(422, "UNREADABLE_DOCUMENT", doc_err, req_id)

    clean = text.strip()
    if len(clean) < MIN_TEXT_CHARS:
        msg = (
            "This looks like a scanned image — no text layer found."
            if has_images
            else "Not enough readable text in this file."
        )
        return fail(422, "UNREADABLE_DOCUMENT", msg, req_id)

    log.info("text_chars=%d elapsed_ms=%d", len(clean), int((time.monotonic() - t0) * 1000))

    parsed, usage, err = await call_perfox(filename, data, ext, clean, build_schema(), build_prompt(date.today()))
    if err:
        status, code, msg = err
        return fail(status, code, msg, req_id)

    parsed, corrected = verify_identity(parsed, clean)
    if corrected:
        log.info("identity corrected: %s", ", ".join(corrected))

    parsed = fix_dates_and_experience(parsed, date.today())
    parsed = tidy_education(parsed)

    parsed, missing = check_required(parsed, clean)
    if missing:
        items = ", ".join(missing)
        msg = f"We couldn't find the following details in this resume: {items}. Please make sure it clearly shows the name, email, and phone number."
        return fail(422, "MISSING_REQUIRED_FIELDS", msg, req_id)

    body = dict(parsed)
    body["usage"] = token_counts(usage)
    return ok(body, req_id)


@router.post("/parsing/jd")
async def parse_jd(
    file: UploadFile | None = File(None),
    jd_text: str = Form(""),
):
    req_id = str(uuid.uuid4())
    t0 = time.monotonic()

    has_file = file is not None and bool((file.filename or "").strip())

    if has_file:
        try:
            filename, ext, data = await read_upload(file, allowed=JD_ALLOWED_EXTS)
        except ValueError as exc:
            status, code, msg = exc.args[0]
            return fail(status, code, msg, req_id)

        text, has_images, doc_err = get_text(data, ext)
        if doc_err:
            return fail(422, "UNREADABLE_DOCUMENT", doc_err, req_id)

        clean = text.strip()
        if len(clean) < MIN_TEXT_CHARS:
            msg = (
                "This looks like a scanned image - no text layer found"
                if has_images
                else "Not enough readable text in this file."
            )
            return fail(422, "UNREADABLE_DOCUMENT", msg, req_id)

        if ext != ".pdf":
            clean = clean[:JD_TEXT_LIMIT]

        log.info("jd ext=%s size=%d text_chars=%d elapsed_ms=%d", ext, len(data), len(clean), int((time.monotonic() - t0) * 1000))

    elif jd_text.strip():
        filename = "jd.txt"
        ext = ".txt"
        clean = jd_text.strip()[:JD_TEXT_LIMIT]
        if len(clean) < MIN_TEXT_CHARS:
            return fail(422, "UNREADABLE_DOCUMENT", "Not enough readable text in this file.", req_id)
        data = clean.encode("utf-8")
        log.info("jd ext=%s size=%d text_chars=%d elapsed_ms=%d", ext, len(data), len(clean), int((time.monotonic() - t0) * 1000))

    else:
        return fail(400, "NO_JOB_DESCRIPTION", "Send a file or paste the job description text.", req_id)

    parsed, usage, err = await call_perfox(filename, data, ext, clean, build_jd_schema(), build_jd_prompt(date.today()))
    if err:
        status, code, msg = err
        return fail(status, code, msg, req_id)

    fixed = fix_jd(parsed, date.today())
    body = dict(fixed)
    body["usage"] = token_counts(usage)
    return ok(body, req_id)


MASK_NOT_A_PDF = "This file type isn't supported for redaction. Please upload a PDF."


def download_header(filename: str) -> str:
    name = f"{Path(filename).stem or 'resume'}_redacted.pdf"
    if re.fullmatch(r"[A-Za-z0-9._-]+", name):
        return f"attachment; filename={name}"
    ascii_name = name.encode("ascii", "ignore").decode().replace('"', "").replace("\\", "") or "resume_redacted.pdf"
    return f"attachment; filename=\"{ascii_name}\"; filename*=UTF-8''{quote(name)}"


@router.post("/parsing/mask")
async def mask_resume(file: UploadFile | None = File(None)):
    req_id = str(uuid.uuid4())
    if file is None or not (file.filename or "").strip():
        return fail(400, "INVALID_REQUEST", "No file was uploaded. Please choose a file and try again.", req_id)
    filename = Path(file.filename).name.strip()
    data = await file.read()
    if len(data) > MAX_FILE_BYTES:
        return fail(400, "INVALID_REQUEST", f"File is too large (limit {MAX_FILE_BYTES // (1024 * 1024)} MB).", req_id)
    if not data or Path(filename).suffix.lower() != ".pdf" or b"%PDF-" not in data[:1024]:
        return fail(400, "UNSUPPORTED_FILE_TYPE", MASK_NOT_A_PDF, req_id)
    try:
        from app.parsing import masking
    except ImportError:
        return fail(500, "MASKING_UNAVAILABLE", "Masking is not available on this server.", req_id)
    try:
        masked, count, warnings = await asyncio.to_thread(masking.mask_pdf, data)
    except masking.CannotOpen:
        return fail(400, "UNSUPPORTED_FILE_TYPE", MASK_NOT_A_PDF, req_id)
    except Exception as exc:
        log.error("masking failed: %s", type(exc).__name__)
        return fail(500, "MASKING_FAILED", "The file could not be masked.", req_id)
    log.info("mask size=%d items=%d warnings=%s", len(data), count, ", ".join(warnings) or "none")
    headers = {"X-Request-ID": req_id, "X-Masked-Items": str(count), "Content-Disposition": download_header(filename)}
    if warnings:
        headers["X-Masking-Warning"] = ", ".join(warnings)
    return Response(content=masked, media_type="application/pdf", headers=headers)
