"""The web app: the pages, the JSON APIs they use, and the Perfox agent's /mcp endpoint.

Pages (app/static): /  the chat-style upload page,  /classic  the original one-button page,  /log  the audit log,
/trace  per-step timings and tokens.
APIs: POST /api/process (upload a resume straight into the pipeline), POST /api/chat (the page's text box, sent to the
Perfox agent), GET /api/config, /api/tools, /api/log, /api/traces, /api/trace/{id}.
Agent: POST /mcp, the MCP tools in app/agent/tools.py.

Run locally with `python run.py`. On Vercel this module is the entrypoint (the `app` below).
"""
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from app import pipeline, settings
from app.agent import chat_bridge, mcp_app, tool_guide
from app.observability import audit, tracing
from app.security import AccessGuard

# No auto-generated /docs, /redoc or /openapi.json: nothing needs them, and this app may face the internet.
app = FastAPI(title="Resume intake", lifespan=mcp_app.lifespan, docs_url=None, redoc_url=None, openapi_url=None)
app.add_middleware(AccessGuard)
app.mount("/static", StaticFiles(directory=settings.STATIC_DIR), name="static")


def _page(name: str) -> FileResponse:
    return FileResponse(settings.STATIC_DIR / name)


# ---------- pages ----------

@app.get("/", include_in_schema=False)
def index():
    return _page("index.html")


@app.get("/classic", include_in_schema=False)
def classic():
    """The first, plain upload page (one button). Kept in case it is wanted back."""
    return _page("classic.html")


@app.get("/log", include_in_schema=False)
def log_page():
    return _page("log.html")


@app.get("/trace", include_in_schema=False)
def trace_page():
    return _page("trace.html")


# ---------- APIs ----------

@app.get("/api/config")
def config():
    """Tells the page how to work: through the Perfox agent (webhook configured) or straight into the pipeline."""
    return {"agent": chat_bridge.configured(), "max_mb": settings.MAX_UPLOAD_MB}


@app.get("/api/tools")
def tools():
    """What the assistant can do, in plain words, for the page's /tools command (see app/agent/tool_guide.py)."""
    return {"agent": chat_bridge.configured(), "groups": tool_guide.TOOL_GUIDE}


@app.post("/api/process")
async def process(file: UploadFile = File(...)):
    data = await file.read()
    return await pipeline.process_file(Path(file.filename or "unnamed").name, data)


@app.post("/api/chat")
async def chat(message: str = Form(""), session_id: str = Form(""), file: UploadFile = File(None)):
    """The page's text box, sent to the Perfox agent (see app/agent/chat_bridge.py). The resume, if any, stays here."""
    if not chat_bridge.configured():
        raise HTTPException(status_code=503, detail="PERFOX_WEBHOOK_URL and PERFOX_WEBHOOK_SECRET are not set")
    upload = None
    if file is not None and file.filename:
        upload = (Path(file.filename).name, await file.read())
    return await chat_bridge.send(session_id, message, upload)


@app.get("/api/log")
def log_rows(limit: int = 200):
    return {"summary": audit.summary(), "rows": audit.recent(max(1, min(limit, 1000)))}


@app.get("/api/traces")
def trace_list(limit: int = 100):
    return {"traces": tracing.recent_traces(max(1, min(limit, 500))), "steps": tracing.step_stats()}


@app.get("/api/trace/{trace_id}")
def trace_detail(trace_id: str):
    found = tracing.get_trace(trace_id)
    if not found:
        raise HTTPException(status_code=404, detail="no trace with this ID")
    return found


# ---------- the Perfox agent: POST /mcp ----------
mcp_app.attach(app)
