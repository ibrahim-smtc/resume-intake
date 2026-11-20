"""FastAPI entrypoint serving the web UI, JSON APIs, and the /mcp server."""
import asyncio
import contextlib
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from app import settings, pipeline, tasks
from app.agent import chat_bridge, forms, intakes, mcp_app, tool_guide
from app.observability import audit, llm_usage, tracing
from app.parsing.router import router as parsing_router
from app.security import AccessGuard

def _prepare_mongodb() -> None:
    try:
        audit.ensure_schema()
    except audit.STORE_ERRORS as exc:  # not fatal: it is tried again on the first write
        print(f"[audit] WARNING: couldn't reach MongoDB at startup ({audit.short(exc)})")


@contextlib.asynccontextmanager
async def lifespan(_app):
    """Creates the MongoDB collections and indexes in the background (so a MongoDB that is down never delays startup), then runs
    the MCP session manager. On the way out, the queued LLM usage is written."""
    setup = asyncio.create_task(asyncio.to_thread(_prepare_mongodb))
    try:
        async with mcp_app.lifespan(_app):
            yield
    finally:
        await setup
        await asyncio.to_thread(llm_usage.flush)


# No auto-generated /docs, /redoc or /openapi.json: nothing needs them, and this app may face the internet.
app = FastAPI(title="Resume intake", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
app.add_middleware(AccessGuard)
app.include_router(parsing_router)
app.mount("/static", StaticFiles(directory=settings.STATIC_DIR), name="static")


def _page(name: str) -> FileResponse:
    return FileResponse(settings.STATIC_DIR / name)


# ---------- pages ----------

@app.get("/", include_in_schema=False)
def index():
    return _page("index.html")


@app.get("/classic", include_in_schema=False)
def classic():
    """Legacy upload page."""
    return _page("classic.html")


@app.get("/favicon.ico", include_in_schema=False)
def favicon():
    """Browsers ask for this by default, whatever the page links to."""
    return FileResponse(settings.STATIC_DIR / "favicon.ico", media_type="image/x-icon")


@app.get("/log", include_in_schema=False)
def log_page():
    return _page("log.html")


@app.get("/trace", include_in_schema=False)
def trace_page():
    return _page("trace.html")


@app.get("/fill/{token}", include_in_schema=False)
def fill_page(token: str):
    """A resume's fill-in form as a page of its own, for a recruiter on WhatsApp (app/agent/forms.py). The page reads the
    token from its address; the token is the form's only key."""
    return FileResponse(settings.STATIC_DIR / "fill.html", headers=forms.PRIVATE)


# ---------- APIs ----------

@app.get("/api/config")
def config():
    """Tells the page how to work: through the Perfox agent (webhook configured) or straight into the pipeline."""
    return {"agent": chat_bridge.configured(), "max_mb": settings.MAX_UPLOAD_MB}


@app.get("/api/tools")
def tools():
    """What the assistant can do, in plain words, for the page's /tools command (see app/agent/tool_guide.py)."""
    return {"agent": chat_bridge.configured(), "groups": tool_guide.TOOL_GUIDE}


def _started(key: str) -> JSONResponse:
    return JSONResponse({"status": "pending", "job": key}, 202)


def _polled(key: str, owner: str = "", headers: dict | None = None) -> JSONResponse:
    code, body = tasks.poll(key, owner)
    return JSONResponse(body, code, headers=headers)


# The three routes below can run longer than Cloudflare waits (100 s, then a 524), so they answer at once with an id and the page
# asks for the result (see app/tasks.py).

@app.post("/api/process")
async def process(file: UploadFile = File(...)):
    # One byte past the limit is enough for the size check to refuse it; a huge upload is never read into memory whole.
    data = await file.read(settings.MAX_UPLOAD_BYTES + 1)
    return _started(tasks.start(_process_answer(Path(file.filename or "unnamed").name, data)))


async def _process_answer(name: str, data: bytes):
    return 200, await pipeline.process_file(name, data)


@app.get("/api/process/{job}")
def process_result(job: str):
    return _polled(job)


@app.post("/api/chat")
async def chat(message: str = Form(""), session_id: str = Form(""), file: UploadFile = File(None)):
    """The page's text box, sent to the Perfox agent (see app/agent/chat_bridge.py). The resume, if any, stays here."""
    if not chat_bridge.configured():
        raise HTTPException(status_code=503, detail="PERFOX_WEBHOOK_URL and PERFOX_WEBHOOK_SECRET are not set")
    upload = None
    if file is not None and file.filename:
        upload = (Path(file.filename).name, await file.read(settings.MAX_UPLOAD_BYTES + 1))  # see process()
    return _started(tasks.start(_chat_answer(session_id, message, upload)))


async def _chat_answer(session_id: str, message: str, upload):
    return 200, await chat_bridge.send(session_id, message, upload)


@app.get("/api/chat/{job}")
def chat_result(job: str):
    return _polled(job)


def _form_intake(token: str):
    intake = intakes.find_by_form(token)
    return intake if intake and intake.kind == "resume" else None


@app.get("/api/form/{token}")
def form_view(token: str):
    """What a fill-in form shows: its fields (a JSON schema), or the result once the profile exists."""
    intake = _form_intake(token)
    if not intake:
        return JSONResponse({"status": "expired", "message": forms.GONE}, 410, headers=forms.PRIVATE)
    return JSONResponse(forms.view(intake), headers=forms.PRIVATE)


@app.post("/api/form/{token}")
async def form_submit(token: str, request: Request):
    """A submitted fill-in form: {"values": {id: text}, "skip": false}. Checked, stored, and the profile created."""
    intake = _form_intake(token)
    if not intake:
        return JSONResponse({"status": "expired", "message": forms.GONE}, 410, headers=forms.PRIVATE)
    try:
        body = await request.json()
    except ValueError:
        body = {}
    key = tasks.start(forms.submit(intake, body if isinstance(body, dict) else {}), owner=token)
    return JSONResponse({"status": "pending", "job": key}, 202, headers=forms.PRIVATE)


@app.get("/api/form/{token}/job/{job}")
def form_submit_result(token: str, job: str):
    """The outcome of a submitted fill-in form: 202 while it is still being worked on, else what the POST used to answer."""
    return _polled(job, owner=token, headers=forms.PRIVATE)


def _store_unavailable(exc: Exception) -> HTTPException:
    print(f"[audit] WARNING: the log pages can't read MongoDB ({audit.short(exc)})")  # short() leaves out credentials
    return HTTPException(status_code=503, detail="audit store unavailable")


@app.get("/api/log")
def log_rows(limit: int = 200):
    try:
        return {"summary": audit.summary(), "rows": audit.recent(max(1, min(limit, 1000)))}
    except audit.STORE_ERRORS as exc:
        raise _store_unavailable(exc)


@app.get("/api/traces")
def trace_list(limit: int = 100):
    try:
        return {"traces": tracing.recent_traces(max(1, min(limit, 500))), "steps": tracing.step_stats()}
    except audit.STORE_ERRORS as exc:
        raise _store_unavailable(exc)


@app.get("/api/trace/{trace_id}")
def trace_detail(trace_id: str):
    try:
        found = tracing.get_trace(trace_id)
    except audit.STORE_ERRORS as exc:
        raise _store_unavailable(exc)
    if not found:
        raise HTTPException(status_code=404, detail="no trace with this ID")
    return found


# ---------- the Perfox agent: POST /mcp ----------
mcp_app.attach(app)
