"""Parsing routes: POST /parsing/resume, /parsing/jd, /parsing/mask, and GET /parsing/health.
The reading itself is in service.py, which the pipeline calls too; this file checks the upload and builds the HTTP answer.
"""
# ── Standard Library ──────────────────────────────────────────────────────────
import asyncio
import logging
import re
import time
import uuid
from datetime import date
from pathlib import Path
from urllib.parse import quote

# ── Third-Party ───────────────────────────────────────────────────────────────
from fastapi import APIRouter, File, Form, UploadFile
from fastapi.responses import JSONResponse, Response

# ── Local Modules ─────────────────────────────────────────────────────────────
from app import settings
from app.parsing import service

# ── 1. Configuration ──────────────────────────────────────────────────────────
MAX_FILE_BYTES: int = 10 * 1024 * 1024  # 10 MB limit
ALLOWED_EXTS: set[str] = {".pdf", ".docx"}
TODAY: str = date.today().isoformat()

JD_ALLOWED_EXTS: set[str] = {".pdf", ".docx", ".txt"}

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


# ── 5. Routes ─────────────────────────────────────────────────────────────────
@router.get("/parsing/health")
def health():
    return {
        "status": "ok",
        "timestamp": time.time(),
        "date": TODAY,
        "perfox_configured": bool(settings.PERFOX_BASE_URL and settings.PERFOX_API_KEY),
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

    body, failure = await service.read_resume(filename, ext, data)
    if failure:
        return fail(*failure, req_id)
    log.info("elapsed_ms=%d", int((time.monotonic() - t0) * 1000))
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
    elif jd_text.strip():
        filename, ext, data = "jd.txt", ".txt", jd_text.strip()[: service.JD_TEXT_LIMIT].encode("utf-8")
    else:
        return fail(400, "NO_JOB_DESCRIPTION", "Send a file or paste the job description text.", req_id)

    body, failure = await service.read_jd(filename, ext, data)
    if failure:
        return fail(*failure, req_id)
    log.info("jd elapsed_ms=%d", int((time.monotonic() - t0) * 1000))
    return ok(body, req_id)


MASK_NOT_A_PDF = "This file type isn't supported for redaction. Please upload a PDF."


def download_header(filename: str) -> str:
    name = f"{Path(filename).stem or 'resume'}_redacted.pdf"
    if re.fullmatch(r"[A-Za-z0-9._-]+", name):
        return f"attachment; filename={name}"
    ascii_name = (
        name.encode("ascii", "ignore").decode().replace('"', "").replace("\\", "")
        or "resume_redacted.pdf"
    )
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
        from app.parsing.masking import masking
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
