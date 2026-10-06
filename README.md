# Resume Intake for Questlight

An AI resume intake agent for **Questlight** (a multi-tenant ATS). A recruiter drops in a resume; the app screens out files that
aren't resumes, reads it, creates the candidate's profile in Questlight, and shows the best matching open roles. If the resume
lacks something Questlight requires (a job title, say), it asks, and the recruiter's answer completes the profile. Recruiters
use it through a chat page, or through a **Perfox** agent that calls the same code as tools.

AI is used **only to read the resume**. Everything else (junk rules, the profile mapping, job matching, the audit log) is plain,
hardcoded code.

```
 resume ─► intake checks ─► "Junk?" ─► parse ─► load into Questlight ─► match open roles
              (size, type)   (rules,    (AI)     (profile created,       (own scoring,
                              no AI)             or: what is missing)     no AI)
                       every step is written to the audit log and traced
```

## Layout

```
app/
  main.py            the web app: pages, JSON APIs, and POST /mcp   (Vercel's entrypoint)
  settings.py        every setting, from environment variables / .env
  security.py        who may reach what (password for the pages, bearer token for /mcp)
  pipeline.py        runs the steps in order: the BRD workflow
  intake/            the workflow blocks
    documents.py       read text out of a PDF or DOCX
    junk.py            "Junk?" rules
    parser_questlight.py, parser_perfox.py     the two resume parsers (switch: RESUME_PARSER)
    questlight.py      fit a parsed resume to Questlight's rules, create the profile
    corrections.py     details a recruiter supplies for what a resume lacked
    matching.py        match open roles
  agent/             the Perfox agent integration
    mcp_app.py         the MCP server and its token-checked /mcp endpoint
    tools.py           the 9 tools the agent can call
    intakes.py         the agent's short-term memory of the files it has taken in
    chat_bridge.py     the page's text box -> the agent's webhook
    formatting.py, downloads.py, tool_guide.py
  observability/     audit.py (the audit log), tracing.py (per-step timings and tokens)
  static/            the pages: index.html (chat), classic.html, log.html, trace.html
tests/               offline test suites + fixtures (python tests/run_all.py)
docs/                perfox-agent-setup.md, architecture diagram
run.py               run it locally
```

## Run it locally

```
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
copy .env.example .env          # then fill in .env (at least QUESTLIGHT_TOKEN)
python run.py                   # http://127.0.0.1:8000
```

Restart after changing `.env`. Pages: `/` the chat page (type `/tools` to see what the assistant can do), `/classic` the original
one-button page, `/log` the audit log, `/trace` per-step timings and tokens.

To let Perfox reach your machine during development, run a Cloudflare quick tunnel (`tools\cloudflared.exe tunnel --url
http://127.0.0.1:8000`). Through a tunnel only `/mcp` is reachable.

## Settings

All of them are in [.env.example](.env.example), with what each means. The ones that matter first:

| Setting | What for |
|---|---|
| `QUESTLIGHT_TOKEN` | Questlight API token (lasts 10 days). Without it no profile is created and no jobs are read |
| `RESUME_PARSER` | `questlight` (default) or `perfox` (temporary stand-in) |
| `MCP_TOKEN` | the bearer token the Perfox agent must send to `/mcp` |
| `PERFOX_WEBHOOK_URL`, `PERFOX_WEBHOOK_SECRET` | connect the chat page to the Perfox agent (without them the page uploads straight into the pipeline) |
| `APP_PASSWORD` | password for the pages and APIs. **Required on Vercel** |

## Tests

```
python tests/run_all.py                 # all offline suites, about 2 minutes
python tests/test_audit.py              # or one at a time
RUN_LIVE=1 python tests/run_all.py      # also the live checks against Questlight's dev services (test_live.py)
```

The offline tests never read your `.env`, never touch a real service and never write to your audit log: Questlight and Perfox are
small mock servers, the parser is faked, and each script uses a throwaway database. `test_live.py` is opt-in; it reads the real
parser and job list and sends profiles that Questlight must reject, so nothing is created.

## Deploying to Vercel

The app is a FastAPI `app` in `app/main.py`, which Vercel detects on its own ([pyproject.toml](pyproject.toml) names it
explicitly, [vercel.json](vercel.json) sets the function's maximum duration to 300 s, [.vercelignore](.vercelignore) keeps tests,
docs and secrets out of the upload).

1. Put the project in a Git repository and import it in Vercel (or run `vercel` from this folder). No build command is needed.
   Check that `.env` is **not** committed (it is in `.gitignore`).
2. Under *Settings > Environment Variables* add the settings from `.env.example`: at least `QUESTLIGHT_TOKEN`, `RESUME_PARSER`,
   `MCP_TOKEN`, `PERFOX_WEBHOOK_URL`, `PERFOX_WEBHOOK_SECRET` and **`APP_PASSWORD`**. Without `APP_PASSWORD` every page and API
   answers 503 on purpose: the app never serves them to the open internet by accident.
3. Deploy, open the URL, and enter the password (any user name).
4. In Perfox, change the MCP integration's URL to `https://<project>.vercel.app/mcp` and click Rediscover. The tunnel is no longer
   needed. The webhook URL doesn't change.

**What to know before relying on it.** I couldn't deploy from here, so none of this has been run on Vercel itself; the settings follow
Vercel's documentation for FastAPI and Python functions.

- **Memory is per instance.** The agent's short-term memory (the file it is working on, the chat upload link) lives in the running
  process, and Vercel may serve a later request from a different instance, or start a new one after a quiet spell. Then the agent
  answers "no file found for that reference" and the resume has to be attached again. Fine for a demo with one user; for real use
  these two stores need a shared store (Redis or Vercel KV), or a host that keeps one long-lived process.
- **The audit log and traces don't persist.** SQLite can only live in `/tmp` there, per instance, and is lost when the instance
  goes away. `/log`, `/trace` and the agent's "what came in" tools will show only what that instance saw. Real use needs a hosted
  database.
- **Uploads are limited to 4 MB** (a Vercel function can't receive more than 4.5 MB), so the limit there is 4 MB by default
  (`MAX_UPLOAD_MB`).
- **Questlight's token expires every 10 days.** Update `QUESTLIGHT_TOKEN` and redeploy (environment changes only apply to new
  deployments). A service account or API key from the Questlight team would remove this chore.
- **Vercel's own "Deployment Protection"** (if switched on for the project) would block Perfox from calling `/mcp`. Use this app's
  password and bearer token instead, or set up Vercel's protection bypass for Perfox.
- **Secrets.** The Perfox API shows a trigger's webhook secret in plain text, and secrets have been pasted around while building
  this. Generate fresh values for production: a new `MCP_TOKEN`, a new webhook Auth Secret, a new `APP_PASSWORD`.

## More

- [docs/perfox-agent-setup.md](docs/perfox-agent-setup.md): registering the MCP server, the agent's prompt, the webhook trigger.
- Not in this repository (it is public): `sources/` (the BRD and Questlight's own documents) and `docs/notes.md` (internal notes on
  Questlight and Perfox behaviour, open decisions). They stay with the team's local copy.
