"""Perfox resume parser service — POST /parsing/resume
Run from repo root:
    python -m uvicorn parsers:app --app-dir perfox-parsers --port 8100 --reload
"""
# ── Standard Library ──────────────────────────────────────────────────────────
import asyncio
import hmac
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
import uvicorn
from dotenv import load_dotenv
from fastapi import FastAPI, File, Form, Request, UploadFile
from fastapi.responses import JSONResponse, Response

# ── Local Modules ─────────────────────────────────────────────────────────────
import documents
from fix_jd import fix_jd
from fix_normlization import check_required, fix_dates_and_experience, tidy_education
from jd_schema import build_jd_prompt, build_jd_schema
from schema import build_prompt, build_schema
from verify_identity import verify_identity

# ── Repo root: where a local run finds .env, and where the tests find the main app's code. The service itself needs nothing from it. ──
REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

# ── Load .env from repo root (not this subdir) ────────────────────────────────
load_dotenv(REPO_ROOT / ".env")

# ── 1. Configuration ──────────────────────────────────────────────────────────
PERFOX_BASE_URL: str = os.getenv("PERFOX_BASE_URL", "").rstrip("/")
PERFOX_API_KEY: str = os.getenv("PERFOX_API_KEY", "")
MAX_FILE_BYTES: int = 10 * 1024 * 1024  # 10 MB limit
ALLOWED_EXTS: set[str] = {".pdf", ".docx"}
TIMEOUT_S: int = 90
MIN_TEXT_CHARS: int = 40  # Below this threshold → UNREADABLE_DOCUMENT
TODAY: str = date.today().isoformat()

JD_ALLOWED_EXTS: set[str] = {".pdf", ".docx", ".txt"}
JD_TEXT_LIMIT: int = 20000

# ── 2. Logging & Error Helper ─────────────────────────────────────────────────
class _SafeFmt(logging.Formatter):
    """Ensures records from third-party libraries don't fail without request_id."""
    def format(self, record: logging.LogRecord) -> str:
        if not hasattr(record, "request_id"):
            record.request_id = "-"
        return super().format(record)


_handler = logging.StreamHandler(sys.stdout)
_handler.setFormatter(
    _SafeFmt("%(asctime)s [%(levelname)s] %(name)s req=%(request_id)s | %(message)s")
)

log = logging.getLogger("perfox")
log.setLevel(logging.INFO)
log.addHandler(_handler)
log.propagate = False


def fail(status: int, code: str, message: str, req_id: str) -> JSONResponse:
    """Single place that builds every error response in Questlight's layout."""
    log.warning("status=%s code=%s msg=%r", status, code, message, extra={"request_id": req_id})
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
    """Single place that builds every successful response with X-Request-ID header."""
    return JSONResponse(content=body, headers={"X-Request-ID": req_id})


# ── 3. Intake Validation ──────────────────────────────────────────────────────
async def read_upload(pdf_doc: UploadFile, allowed: set[str] = ALLOWED_EXTS) -> tuple[str, str, bytes]:
    """Validates the upload and returns (filename, ext, raw_bytes).
    Raises ValueError((status, code, message)) on any validation failure
    so the route can catch it in one place and call fail().
    """

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

# ── 4. Document Text Extraction ───────────────────────────────────────────────
def get_text(data: bytes, ext: str) -> tuple[str, bool, str | None]:
    """Extracts readable text locally. Returns (text, has_images, error).
    Delegates to documents.py (a copy of the main app's reader) which handles PDF, DOCX, TXT and old .doc.
    has_images is True when a PDF has image layers but no text (scanned).
    error is a human-readable string when the file can't be read.
    """
    return documents.extract_text(data, ext)

# ── 5. Perfox API Client ──────────────────────────────────────────────────────
async def call_perfox(
    name: str, data: bytes, ext: str, text: str, schema: dict, prompt: str
) -> tuple[dict | None, dict | None, tuple[int, str, str] | None]:
    """Sends document to Perfox /documents/extract_structured with the given schema and prompt.
    Returns (parsed_data, usage, None) on success (usage is Perfox's {input_tokens, output_tokens}, or None if it sent none),
    or (None, None, (status_code, error_code, message)) on error.
    """
    if not PERFOX_BASE_URL or not PERFOX_API_KEY:
        return None, None, (500, "CONFIG_ERROR", "Perfox API credentials are not configured in .env")

    # Clean bearer token if provided with prefix
    key = PERFOX_API_KEY[7:] if PERFOX_API_KEY.lower().startswith("bearer") else PERFOX_API_KEY

    # Non-PDF files (DOCX, TXT) go as text/plain with .txt filename
    if ext == ".pdf":
        upload_data = data
        upload_name = name
        mime = "application/pdf"
    else:
        upload_data = text.encode("utf-8")
        upload_name = name if name.endswith(".txt") else name + ".txt"
        mime = "text/plain"

    payload_data = {
        "schema": json.dumps(schema),
        "prompt": prompt
    }

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

# ── 6. FastAPI Application & Routes ───────────────────────────────────────────
def token_counts(usage: dict | None) -> dict | None:
    """Perfox's token counts for one parse as {input_tokens, output_tokens, total_tokens}, or None if it sent none."""
    if not usage:
        return None
    try:
        tokens_in, tokens_out = int(usage["input_tokens"]), int(usage["output_tokens"])
    except (TypeError, KeyError, ValueError):
        return None
    return {"input_tokens": tokens_in, "output_tokens": tokens_out, "total_tokens": tokens_in + tokens_out}


app = FastAPI(
    title="Perfox Parsing Service",
    description=("Resume and job description parsing via Perfox, and resume masking, shaped like Questlight's parsing service. "
                 "When the SERVICE_TOKEN setting is present every /parsing/ call needs the header 'Authorization: Bearer <token>'."),
    version="1.0.0",
)


@app.middleware("http")
async def require_token(request: Request, call_next):
    """When the SERVICE_TOKEN setting is present, every /parsing/ call must carry it as 'Authorization: Bearer <token>' (checked before the
    upload is even read). Without the setting the service is open, as it is when run locally. /health stays open. Read on every call, so
    changing the setting takes effect at once."""
    token = os.getenv("SERVICE_TOKEN", "")
    if token and request.url.path.startswith("/parsing/"):
        scheme, _, sent = request.headers.get("authorization", "").partition(" ")
        if scheme.lower() != "bearer" or not hmac.compare_digest(sent.strip().encode(), token.encode()):
            response = fail(401, "UNAUTHORIZED", "A valid token is required. Send it as 'Authorization: Bearer <token>'.", str(uuid.uuid4()))
            response.headers["WWW-Authenticate"] = "Bearer"
            return response
    return await call_next(request)


@app.get("/health")
def health():
    return {
        "status": "ok",
        "timestamp": time.time(),
        "date": TODAY,
        "perfox_configured": bool(PERFOX_BASE_URL and PERFOX_API_KEY),
    }


@app.post("/parsing/resume")
async def parse_resume(pdf_doc: UploadFile = File(...)):
    req_id = str(uuid.uuid4())
    t0 = time.monotonic()

    # 1. Intake Validation
    try:
        filename, ext, data = await read_upload(pdf_doc)
    except ValueError as exc:
        status, code, msg = exc.args[0]
        return fail(status, code, msg, req_id)

    log.info("upload ext=%s size=%d", ext, len(data), extra={"request_id": req_id})

    # 2. Text Extraction
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

    log.info(
        "text_chars=%d elapsed_ms=%d",
        len(clean),
        int((time.monotonic() - t0) * 1000),
        extra={"request_id": req_id},
    )

    # 3. Perfox Extraction Call
    parsed, usage, err = await call_perfox(filename, data, ext, clean, build_schema(), build_prompt(date.today()))
    if err:
        status, code, msg = err
        return fail(status, code, msg, req_id)

    # 4. Check name, e-mail and phone against the document's own text (Perfox can misread a PDF's letters or tidy a number)
    parsed, corrected = verify_identity(parsed, clean)
    if corrected:
        log.info("identity corrected: %s", ", ".join(corrected), extra={"request_id": req_id})  # which fields, never their values

    # 5. Normalize Dates & Experience, and drop education entries that have no institution
    parsed = fix_dates_and_experience(parsed, date.today())
    parsed = tidy_education(parsed)

    # 6. Validate Required Fields (Name, Email, Phone)
    parsed, missing = check_required(parsed, clean)
    if missing:
        items = ", ".join(missing)
        msg = f"We couldn't find the following details in this resume: {items}. Please make sure it clearly shows the name, email, and phone number."
        return fail(422, "MISSING_REQUIRED_FIELDS", msg, req_id)

    body = dict(parsed)
    body["usage"] = token_counts(usage)
    return ok(body, req_id)



@app.post("/parsing/jd")
async def parse_jd(
    file: UploadFile | None = File(None),
    jd_text: str = Form(""),
):
    req_id = str(uuid.uuid4())
    t0 = time.monotonic()

    has_file = file is not None and bool((file.filename or "").strip())

    if has_file:
        # File provided: validate and extract
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

        log.info(
            "jd ext=%s size=%d text_chars=%d elapsed_ms=%d",
            ext,
            len(data),
            len(clean),
            int((time.monotonic() - t0) * 1000),
            extra={"request_id": req_id},
        )

    elif jd_text.strip():
        # Pasted text provided
        filename = "jd.txt"
        ext = ".txt"
        clean = jd_text.strip()[:JD_TEXT_LIMIT]
        if len(clean) < MIN_TEXT_CHARS:
            return fail(422, "UNREADABLE_DOCUMENT", "Not enough readable text in this file.", req_id)
        data = clean.encode("utf-8")

        log.info(
            "jd ext=%s size=%d text_chars=%d elapsed_ms=%d",
            ext,
            len(data),
            len(clean),
            int((time.monotonic() - t0) * 1000),
            extra={"request_id": req_id},
        )

    else:
        return fail(400, "NO_JOB_DESCRIPTION", "Send a file or paste the job description text.", req_id)



    # Call Perfox with JD Schema and Prompt
    parsed, usage, err = await call_perfox(
        filename, data, ext, clean, build_jd_schema(), build_jd_prompt(date.today())
    )
    if err:
        status, code, msg = err
        return fail(status, code, msg, req_id)

    # Normalize to exactly 30 fields
    fixed = fix_jd(parsed, date.today())
    body = dict(fixed)
    body["usage"] = token_counts(usage)
    return ok(body, req_id)


MASK_NOT_A_PDF = "This file type isn't supported for redaction. Please upload a PDF."


def download_header(filename: str) -> str:
    """Content-Disposition for the masked file: "<name without extension>_redacted.pdf", as Questlight names it. Plain when the name is simple
    letters, digits, dots, dashes and underscores; otherwise quoted, with a UTF-8 copy for names that are not plain ASCII."""
    name = f"{Path(filename).stem or 'resume'}_redacted.pdf"
    if re.fullmatch(r"[A-Za-z0-9._-]+", name):
        return f"attachment; filename={name}"
    ascii_name = name.encode("ascii", "ignore").decode().replace('"', "").replace("\\", "") or "resume_redacted.pdf"
    return f"attachment; filename=\"{ascii_name}\"; filename*=UTF-8''{quote(name)}"


@app.post("/parsing/mask")
async def mask_resume(file: UploadFile | None = File(None)):
    """A resume PDF in, the same PDF out with its contact details removed in place (see masking.py), like Questlight's /parsing/mask.
    No AI is involved. A scan or a PDF with nothing to mask is returned as it is (200), as Questlight does; warnings only say so in a header."""
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
        import masking  # imported here so the other routes keep working on a machine without the PDF library
    except ImportError:
        return fail(500, "MASKING_UNAVAILABLE", "Masking is not available on this server.", req_id)
    try:
        masked, count, warnings = await asyncio.to_thread(masking.mask_pdf, data)  # PDF work is CPU work: keep the server free
    except masking.CannotOpen:
        return fail(400, "UNSUPPORTED_FILE_TYPE", MASK_NOT_A_PDF, req_id)
    except Exception as exc:
        log.error("masking failed: %s", type(exc).__name__, extra={"request_id": req_id})  # the kind of error only, never document content
        return fail(500, "MASKING_FAILED", "The file could not be masked.", req_id)
    log.info("mask size=%d items=%d warnings=%s", len(data), count, ", ".join(warnings) or "none", extra={"request_id": req_id})
    headers = {"X-Request-ID": req_id, "X-Masked-Items": str(count), "Content-Disposition": download_header(filename)}
    if warnings:
        headers["X-Masking-Warning"] = ", ".join(warnings)
    return Response(content=masked, media_type="application/pdf", headers=headers)


# ── 7. Server Entrypoint ──────────────────────────────────────────────────────
if __name__ == "__main__":
    uvicorn.run(
        "parsers:app",
        host="127.0.0.1",
        port=8100,
        reload=True,
        app_dir=str(REPO_ROOT / "perfox-parsers"),
        reload_dirs=[str(REPO_ROOT / "perfox-parsers")],
    )
