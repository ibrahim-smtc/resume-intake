# Questlight Resume & JD Intake MCP Server

FastAPI application and MCP server integrating **Perfox** document extraction with **Questlight** ATS workflows.

Recruiters interact via a Perfox agent (WhatsApp or chat over `POST /mcp`) or the built-in web UI (`/`). Uploaded resumes and job descriptions are validated locally, parsed via Perfox `extract_structured`, loaded into Questlight, and matched/screened against open roles or existing candidates.

## Workflow Overview

- **Resume Intake (`app/pipeline.py`):** File validation (size, type, ZIP/DOCX bounds) -> rule-based junk/JD classification -> existing-candidate check by email (`GET /applicants/{email}`) -> structured extraction -> Questlight profile creation (or fill-in form `/fill/{token}` when required fields are missing) -> open-role scoring and top-3 Screening attachment.
- **JD Intake (`app/job_pipeline.py`):** File/text validation -> JD extraction -> duplicate open-job check -> Questlight job creation -> semantic matching trigger -> ranking existing candidates and Screening strong matches.

## Project Structure

```text
app/
  main.py            FastAPI entrypoint (MCP mount, JSON APIs, web pages)
  settings.py        Environment configuration loader
  security.py        HTTP Basic guard and Cloudflare tunnel isolation
  net.py             Shared SSL context httpx client factory
  pipeline.py        Resume intake orchestrator
  job_pipeline.py    Job description intake orchestrator
  intake/            Validation, junk rules, Questlight API client, matching & ranking
  parsing/           Perfox extraction client, normalization, verification, /parsing/* routes
  agent/             FastMCP server (/mcp), 9 MCP tools, session store, /fill/{token}, WhatsApp notify
  observability/     SQLite audit log (/log) and span tracing (/trace)
  static/            Web UI, audit/trace viewers, and fill-in form assets
docs/
  perfox-agent-setup.md   Perfox MCP integration, prompt, and webhook configuration
run.py               Local development server (uvicorn)
```

## Local Setup

```bash
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
copy .env.example .env
python run.py
```

Server starts at `http://127.0.0.1:8000` (`/` chat UI, `/classic` direct upload, `/log` audit log, `/trace` step timings, `/mcp` MCP endpoint).

For local Perfox testing over HTTPS, expose port `8000` with Cloudflare quick tunnel (`cloudflared tunnel --url http://127.0.0.1:8000`). Without `APP_PASSWORD`, tunnel requests are restricted to `/mcp`.

## Environment Variables

See [`.env.example`](.env.example) for the complete list. Key variables:

| Variable | Purpose |
|---|---|
| `QUESTLIGHT_TOKEN` | Questlight API bearer token (10-day validity) |
| `PERFOX_API_KEY`, `PERFOX_BASE_URL` | Perfox API credentials (`documents:extract` scope) |
| `MCP_TOKEN` | Bearer token enforced on `POST /mcp` |
| `PERFOX_WEBHOOK_URL`, `PERFOX_WEBHOOK_SECRET` | Connects the web chat UI (`/api/chat`) to the Perfox webhook trigger |
| `PERFOX_WHATSAPP_FROM`, `PERFOX_MESSAGING_KEY` | Optional WhatsApp notification after `/fill/{token}` submission |
| `APP_PASSWORD` | Optional HTTP Basic Auth password protecting UI and JSON routes |

## Vercel Deployment

Configured in [`pyproject.toml`](pyproject.toml) (`app.main:app` entrypoint) and [`vercel.json`](vercel.json) (`bom1` region, `maxDuration: 300`).

1. Import the repository in Vercel and configure environment variables from [`.env.example`](.env.example).
2. Protect `/mcp` with `MCP_TOKEN` and set `APP_PASSWORD` before sharing public URLs. Disable Vercel Deployment Protection (or configure protection bypass) so Perfox can reach `/mcp`.
3. Register `https://<project>.vercel.app/mcp` in Perfox (see [`docs/perfox-agent-setup.md`](docs/perfox-agent-setup.md)).

### Runtime Notes
- **Stateless instances:** In-memory intake state (`Intake` 30-minute cache) and `/tmp/audit.db` live per serverless instance; multi-instance production deployments require external storage (e.g., Redis/Postgres).
- **Upload limit:** Vercel request bodies cap at 4.5 MB (`MAX_UPLOAD_MB` defaults to `4` on Vercel).
- **Long-running tools:** Perfox enforces a 30s client timeout on MCP tool calls; long parse/create calls return `status: "still_working"` at 24s (`PROCESS_SOFT_DEADLINE_S`) while continuing in the background so the follow-up tool call joins the completed result.
