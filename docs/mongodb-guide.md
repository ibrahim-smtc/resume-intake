# MongoDB in the resume intake app: what it does and how to set it up

## What we did
The app used to keep its audit log and step timings in a small SQLite file. Now it keeps them in MongoDB. It also records how many tokens each Perfox call used.

Nothing in the data holds resume text or candidate details. Times are saved in Indian time (+05:30).

## What gets saved

**In the app's own database** (`MONGODB_URI` / `MONGODB_DB`). The app creates these itself:

| Collection | What is in it |
|---|---|
| `audit_log` | One row for each decision on an upload (accepted, junk, created, and so on) |
| `trace_spans` | One row for each step of an upload, with how long it took |
| `llm_perfox_usage_events` | One row for each Perfox call: tokens, time, status |
| `llm_perfox_usage_daily` | The same usage added up per day |

**In Questlight's usage database** (optional, `LLM_USAGE_MONGO_URL` / `LLM_USAGE_DB_NAME`). This is for their AI cost dashboards:

| Collection | What is in it |
|---|---|
| `llm_usage_events` | One row for each Perfox call, in Questlight's format |
| `llm_usage_daily` | Daily totals per tenant, service, operation and model |

Each Perfox call ends with one of six statuses: `ok`, `error`, `rate_limited`, `timeout`, `connection`, `cancelled`.

## What happens when MongoDB has a problem
- If it is down or the login is wrong, the app waits 5 seconds, then stops trying for 60 seconds. Uploads still work, but the log and traces are not saved. A warning is printed.
- The `/log` and `/trace` pages answer "503 audit store unavailable" in that case.
- For Questlight's usage database, if a write fails, the records are printed in the logs as `LLM_USAGE_EVENT {...}` lines, and that database is skipped for 30 seconds.

## How to set it up

### 1. The app's own MongoDB (required)
1. Get a MongoDB with a login turned on.
2. Set `MONGODB_URI` to its connection string (with the user and password).
3. Set `MONGODB_DB` to the database name (for production, use the production database).
4. The user needs read and write on that database. Collections and indexes are created on first use. If the user can't create indexes, the app prints one warning and carries on. Ask a DBA to create them (the list is in the README).
5. Start the app. The log is saved from the first upload.

Keep the connection string out of git and out of logs.

### 2. Questlight's usage database (optional, only after Questlight answers)
1. Ask the Questlight team for the connection URL, the database name, and a user that can only insert and update.
2. Ask them to run their `scripts/create-llm-usage-indexes.js` on that database. The app never creates indexes there.
3. Set `LLM_USAGE_MONGO_URL` and `LLM_USAGE_DB_NAME`. Both are needed to switch it on.
4. Optional settings: `LLM_USAGE_SERVICE_NAME` (default `resume-agent-mcp`), `LLM_USAGE_PROVIDER` (default `perfox`), `PERFOX_EXTRACT_MODEL` (the model name, if known).
5. Restart the app, parse a fake resume, and check that a row appears in `llm_usage_events`.

## What is not done
- **Cost.** Perfox only sends input and output tokens, with no model name and no cost. Cost is added once Perfox sends it.
- **The Perfox agent's own chat tokens.** They are used inside Perfox, so only Perfox can report them.
- **Questlight's answers.** We still need to know if we write straight to their MongoDB or through their API, and which operation names they want.

## Run only one copy of the app
The app keeps its working state in memory, so run one replica with one worker.
