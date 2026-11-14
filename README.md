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
  security.py        HTTP Basic guard (optional password)
  net.py             Shared SSL context httpx client factory
  pipeline.py        Resume intake orchestrator
  job_pipeline.py    Job description intake orchestrator
  intake/            Validation, junk rules, Questlight API client, matching & ranking
  parsing/           Perfox extraction client, normalization, verification, /parsing/* routes
  agent/             FastMCP server (/mcp), 9 MCP tools, session store, /fill/{token}, WhatsApp notify
  observability/     MongoDB audit log (/log), span tracing (/trace) and Perfox token usage
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

A MongoDB must be running for the audit log and traces (locally: install MongoDB Community Server, e.g. `winget install MongoDB.Server`; the default URI is `mongodb://localhost:27017`). If it is down, uploads still work but the audit log, traces and usage are not recorded, and a warning is printed.

Server starts at `http://127.0.0.1:8000` (`/` chat UI, `/classic` direct upload, `/log` audit log, `/trace` step timings, `/mcp` MCP endpoint).

For local Perfox testing over HTTPS, expose port `8000` with Cloudflare quick tunnel (`cloudflared tunnel --url http://127.0.0.1:8000`). Without `APP_PASSWORD` the pages are open, tunnel or not.

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

## Deploying to a company cloud or container

The app is a plain ASGI application (`app.main:app`, Python 3.12+). Start it with:

```bash
pip install -r requirements.txt
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

`run.py` is for development only (it binds to 127.0.0.1 and reloads on change). Configuration is by environment variables only (see [`.env.example`](.env.example)); no `.env` file is needed in a container (one is only read if it exists). `vercel.json`, `.vercelignore` and `[tool.vercel]` in `pyproject.toml` apply to Vercel only and can be ignored elsewhere. Logs go to stdout and stderr.

**Run exactly one replica with one worker.** The agent's working state lives in the memory of the process: the files it is working on, the fill-in form links, the "still working, call again" bookkeeping and the job and candidate caches. A second worker or replica would not see another's state (Perfox's tool calls cannot be pinned to one instance), so a follow-up call would answer "no file found for that reference". Running more than one needs that state moved to a shared store such as Redis first. A restart loses whatever was in progress (files are kept for 30 minutes anyway); a recruiter just sends the file again.

**Settings that must be set** (the defaults suit development, not production):

| Setting | Why |
|---|---|
| `QUESTLIGHT_BASE_URL`, `MATCHING_BASE_URL` | Both default to `dev-api.quest-light.com`; unset, the app writes to the dev system |
| `QUESTLIGHT_TOKEN` | Valid for 10 days. Ask the Questlight team for a service account or API key; until then it must be renewed and the app restarted |
| `PUBLIC_BASE_URL` | The address people reach the app at, e.g. `https://intake.example.com`. The app only learns its own address on Vercel; without this the fill-in form links sent on WhatsApp are `/fill/<token>` with no host and do not open on a phone |
| `MCP_TOKEN` | Bearer token required on `POST /mcp` (register the same value in the Perfox integration) |
| `APP_PASSWORD` | Without it the pages, the audit log and the upload APIs are open to anyone who can reach the URL. `/parsing/*` has no login in any case: keep it off the public internet, or put the company's SSO or ingress authentication in front |
| `MCP_FILE_HOSTS` | Comma-separated hosts the agent may download resumes from. Empty means any https host |
| `MONGODB_URI` | Connection string of the MongoDB that holds the audit log, traces and Perfox usage (default `mongodb://localhost:27017`). Hosts, replica set, SRV, credentials, TLS, pool size and timeouts all go in the string. Use a MongoDB with authentication turned on, and keep the string out of git and logs. A MongoDB that is down is given up on after 5 s (set `serverSelectionTimeoutMS` in the string to change that) and is not tried again for 60 s, so intake carries on without logging instead of stalling; each skipped write prints an `[audit] WARNING` line |
| `MONGODB_DB` | Database name. Default: the database named in the URI, else `questlight_local` |

**Network:**
- The ingress or load balancer must allow request bodies up to `MAX_UPLOAD_MB` (default 5 MB; nginx's default limit is 1 MB, which would refuse most resumes) and wait at least **300 s** for a response: the chat page waits up to 280 s for the agent, and creating a job from a JD can take two minutes inside Questlight.
- Outbound HTTPS is needed to the Perfox API, the Questlight API and matching service, and the file storage Perfox links point to.
- `GET /parsing/health` answers 200 without a login and suits a liveness probe.

## Vercel Deployment

Configured in [`pyproject.toml`](pyproject.toml) (`app.main:app` entrypoint) and [`vercel.json`](vercel.json) (`bom1` region, `maxDuration: 300`).

1. Import the repository in Vercel and configure environment variables from [`.env.example`](.env.example).
2. Protect `/mcp` with `MCP_TOKEN` and set `APP_PASSWORD` before sharing public URLs. Disable Vercel Deployment Protection (or configure protection bypass) so Perfox can reach `/mcp`.
3. Register `https://<project>.vercel.app/mcp` in Perfox (see [`docs/perfox-agent-setup.md`](docs/perfox-agent-setup.md)).

### Runtime Notes
- **Stateless instances:** In-memory intake state (`Intake` 30-minute cache) lives per serverless instance, so multi-instance production deployments require external storage (e.g., Redis). The audit log, traces and usage are in MongoDB (`MONGODB_URI`), which Vercel must be able to reach.
- **Upload limit:** Vercel request bodies cap at 4.5 MB (`MAX_UPLOAD_MB` defaults to `4` on Vercel).
- **Long-running tools:** Perfox enforces a 30s client timeout on MCP tool calls; long parse/create calls return `status: "still_working"` at 24s (`PROCESS_SOFT_DEADLINE_S`) while continuing in the background so the follow-up tool call joins the completed result.
