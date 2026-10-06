"""Step tracing end to end, with mocks of Perfox (the parser) and Questlight. Every upload is a trace; every step a span with its
time, status and, for LLM calls, tokens. Nothing real is called."""
import asyncio
import json
import os
import sqlite3

import harness

tmp = harness.isolate(QUESTLIGHT_TOKEN="good-token", PERFOX_API_KEY="test-key", RESUME_PARSER="perfox", QUESTLIGHT_ATTACH_RESUME="true")
from harness import JUNK_FILES, check, finish, serve  # noqa: E402

from fastapi import FastAPI, Request  # noqa: E402
from fastapi.responses import JSONResponse  # noqa: E402

from fixtures.jobs import JOBS  # noqa: E402
from fixtures.parsed import PRIYA, variant  # noqa: E402

PARSED = variant(PRIYA, email="priya.secret@example.com", technical_skills=["Java", "Spring Boot", "Docker"], total_experience={"years": 0, "months": 0},
                 workExperience=[{"companyName": "Acme", "jobTitle": "Software Engineer", "location": "", "startDate": "2019-01-01",
                                  "endDate": "2026-01-01", "duration": {"years": 0, "months": 0}, "responsibilities": ["secret-project-x"]}])
mock_state = {"usage": True, "perfox_status": 200, "jobs_calls": 0}
mock = FastAPI()


@mock.post("/perfox/documents/extract_structured")
async def perfox(request: Request):
    await request.form()
    await asyncio.sleep(0.3)  # pretend the LLM takes a while
    if mock_state["perfox_status"] != 200:
        return JSONResponse({"error": "internal", "message": "model failed"}, mock_state["perfox_status"])
    body = {"data": json.loads(json.dumps(PARSED))}
    if mock_state["usage"]:
        body["usage"] = {"input_tokens": 370, "output_tokens": 571}
    return body


@mock.post("/api/applicants/create-applicant")
async def create(request: Request):
    await request.form()
    await asyncio.sleep(0.05)
    return JSONResponse({"data": {"_id": "u1", "applicantId": "CAN-051026-00099"}}, 201)


@mock.post("/api/jobs/applicantMatching/create")
async def screen():
    return JSONResponse({"data": {}}, 201)


@mock.post("/api/applicants/change-status")
async def change_status():
    return JSONResponse({"data": {}}, 200)


@mock.get("/api/jobs/all")
async def jobs_all():
    mock_state["jobs_calls"] += 1
    await asyncio.sleep(0.1)
    return {"data": JOBS}


server = serve(mock)
os.environ["QUESTLIGHT_BASE_URL"] = server.url + "/api"
os.environ["PERFOX_BASE_URL"] = server.url + "/perfox"   # read when the parser module is imported, so set before it

import httpx  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402

client = TestClient(app)


def upload(fname, data=None, name=None):
    body = data if data is not None else (JUNK_FILES / fname).read_bytes()
    return client.post("/api/process", files={"file": (name or fname, body)}).json()


# 1. a full run
r = upload("r01_standard.pdf")
t = r["trace"]
spans = {s["name"]: s for s in t["spans"]}
expected = ["resume intake", "intake checks", "junk check", "parse resume", "load into Questlight", "POST create-applicant",
            "match open roles", "fetch open jobs", "score jobs", "screen top roles",
            "POST applicantMatching/create", "POST applicants/change-status",   # the new candidate's status goes to SCREENING once
            "POST applicantMatching/create", "POST applicantMatching/create"]
check("steps in the right order", [s["name"] for s in t["spans"]] == expected, [s["name"] for s in t["spans"]])
root = t["spans"][0]
check("children hang under the right parents",
      spans["POST create-applicant"]["parent_id"] == spans["load into Questlight"]["id"]
      and spans["fetch open jobs"]["parent_id"] == spans["match open roles"]["id"]
      and spans["score jobs"]["parent_id"] == spans["match open roles"]["id"]
      and all(spans[n]["parent_id"] == root["id"] for n in ("intake checks", "junk check", "parse resume", "load into Questlight", "match open roles", "screen top roles"))
      and all(s["parent_id"] == spans["screen top roles"]["id"] for s in t["spans"] if s["name"] == "POST applicantMatching/create"))
p = spans["parse resume"]
check("parse is an llm step with the provider's token counts", p["kind"] == "llm" and p["input_tokens"] == 370 and p["output_tokens"] == 571
      and p["provider"] == "perfox" and p["model"] is None)
check("parse time includes the 300 ms the mock LLM took", p["duration_ms"] >= 300)
check("totals: 941 tokens, 1 llm call", t["input_tokens"] + t["output_tokens"] == 941 and t["llm_calls"] == 1)
check("the root's time covers its children", root["duration_ms"] >= sum(s["duration_ms"] for s in t["spans"] if s["parent_id"] == root["id"]) - 1)
check("other steps used no tokens", all(s["input_tokens"] is None for s in t["spans"] if s["kind"] != "llm"))
check("the first job list came from Questlight, not the cache", spans["fetch open jobs"]["details"].get("from_cache") is False)

saved = client.get(f"/api/trace/{t['trace_id']}").json()
check("the trace is saved and read back with the same steps", [s["name"] for s in saved["spans"]] == expected and saved["file"] == "r01_standard.pdf")
check("the audit log's run_id links to the trace", any(x["run_id"] == t["trace_id"] for x in client.get("/api/log").json()["rows"]))
check("an unknown trace id gives 404", client.get("/api/trace/nope").status_code == 404)
check("the trace page and its script are served", client.get("/trace").status_code == 200 and client.get("/static/trace-view.js").status_code == 200)

# 2. the second run uses the cached job list
r2 = upload("r02_standard.docx")
s2 = {s["name"]: s for s in r2["trace"]["spans"]}
check("second run: job list from cache, no second download", s2["fetch open jobs"]["details"].get("from_cache") is True and mock_state["jobs_calls"] == 1)

# 3. junk stops early: no parse step, no tokens
rj = upload("j06_invoice.pdf")
check("junk file: only the intake and junk steps, 0 tokens",
      [s["name"] for s in rj["trace"]["spans"]] == ["resume intake", "intake checks", "junk check"] and rj["trace"]["input_tokens"] == 0)

# 4. errors are marked on the step that failed and on the root
rc = upload("x", data=b"%PDF-1.4 broken", name="corrupt.pdf")
sc = {s["name"]: s for s in rc["trace"]["spans"]}
check("corrupt PDF: the junk check step and the root are marked error", sc["junk check"]["status"] == "error" and rc["trace"]["spans"][0]["status"] == "error")
rt = upload("x", data=b"hello", name="photo.png")
check("wrong type: the intake checks step is marked error", {s["name"]: s for s in rt["trace"]["spans"]}["intake checks"]["status"] == "error")
mock_state["perfox_status"] = 500
rp = upload("r01_standard.pdf")
sp = {s["name"]: s for s in rp["trace"]["spans"]}
check("parser 500: the parse step is marked error with the reason, no tokens", sp["parse resume"]["status"] == "error"
      and "Perfox rejected" in sp["parse resume"]["error"] and sp["parse resume"]["input_tokens"] is None)
mock_state["perfox_status"] = 200

# 5. a provider that reports no usage
mock_state["usage"] = False
ru = upload("r01_standard.pdf")
su = {s["name"]: s for s in ru["trace"]["spans"]}
check("no usage reported: shown as 'not reported' and counted", su["parse resume"]["input_tokens"] is None
      and su["parse resume"]["details"].get("tokens") == "not reported by Perfox" and ru["trace"]["llm_calls_without_usage"] == 1)
mock_state["usage"] = True


# 6. two uploads at the same time don't mix their steps
async def two_at_once():
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t", timeout=60) as c:
        a, b = await asyncio.gather(
            c.post("/api/process", files={"file": ("r01_standard.pdf", (JUNK_FILES / "r01_standard.pdf").read_bytes())}),
            c.post("/api/process", files={"file": ("j06_invoice.pdf", (JUNK_FILES / "j06_invoice.pdf").read_bytes())}))
        return a.json()["trace"], b.json()["trace"]


ta, tb = asyncio.run(two_at_once())
check("parallel uploads: each trace has only its own steps",
      len(ta["spans"]) == len(expected) and len(tb["spans"]) == 3 and ta["trace_id"] != tb["trace_id"]
      and not ({s["id"] for s in ta["spans"]} & {s["id"] for s in tb["spans"]}))

# 7. no candidate data in the trace store
dump = "\n".join(sqlite3.connect(os.environ["AUDIT_DB"]).iterdump())
leaks = [x for x in ("priya.secret", "98765", "secret-project-x", "Anna University", "Priya") if x in dump]
check(f"no candidate data stored in traces (found: {leaks})", not leaks)

# 8. per-step statistics
stats = client.get("/api/traces").json()
names = [s["name"] for s in stats["steps"]]
pr = next(s for s in stats["steps"] if s["name"] == "parse resume")
check("per-step stats include every step", {"score jobs", "fetch open jobs", "junk check"} <= set(names), names)
check("parse tokens are summed over the runs that reported them (3 of them)", pr["input_tokens"] == 370 * 3, pr)
check("the stats response carries no LangSmith flag any more", "langsmith" not in stats)

# 9. a broken trace store must not break the upload
os.environ["AUDIT_DB"] = str(tmp)  # a folder, not a file
rb = upload("r01_standard.pdf")
check("broken trace store: the upload completes and still returns its trace", rb["ok"] and rb["profile"]["status"] == "created" and rb["trace"]["spans"])
finish()
