"""The "Match open roles" step: scoring, which jobs count as open, "no strong match", and what happens when Questlight's job list
is down. A mock Questlight serves hand-made jobs (tests/fixtures/jobs.py); the parser is stubbed."""
import asyncio
import copy
import os

import harness

harness.isolate(QUESTLIGHT_TOKEN="good-token", RESUME_PARSER="perfox")
from harness import JUNK_FILES, check, finish, serve  # noqa: E402

from fastapi import FastAPI  # noqa: E402
from fastapi.responses import JSONResponse  # noqa: E402

from fixtures.jobs import JOBS, OPEN_JOB_COUNT  # noqa: E402
from fixtures.parsed import CHEM, JAVA, SALES, variant  # noqa: E402

state = {"jobs_status": 200, "screen": {}, "screen_calls": []}  # screen: job _id -> HTTP status to answer with (default 201)
mock = FastAPI()


@mock.get("/api/jobs/all")
async def jobs_all():
    if state["jobs_status"] != 200:
        return JSONResponse({"message": "boom"}, state["jobs_status"])
    return {"statusCode": 200, "data": JOBS}


@mock.post("/api/applicants/create-applicant")
async def create():
    return JSONResponse({"data": {"_id": "u1", "applicantId": "CAN-031026-00042"}}, 201)


@mock.post("/api/jobs/applicantMatching/create")
async def screen(body: dict):
    state["screen_calls"].append(body)
    code = state["screen"].get(body["jobId"], 201)
    return JSONResponse({"message": "no"} if code >= 400 else {"data": {}}, code)


questlight = serve(mock)
os.environ["QUESTLIGHT_BASE_URL"] = questlight.url + "/api"

from fastapi.testclient import TestClient  # noqa: E402

from app import pipeline  # noqa: E402
from app.intake import matching, parser_perfox  # noqa: E402
from app.observability import audit  # noqa: E402
from app.main import app  # noqa: E402

client = TestClient(app)
current = {}


async def stub(name, data, ext):
    return copy.deepcopy(current["parsed"]), None


parser_perfox.parse_resume = stub


def upload(parsed, pdf="r01_standard.pdf"):
    matching._cache["at"] = 0   # force a fresh download of the job list
    current["parsed"] = parsed
    return client.post("/api/process", files={"file": (pdf, (JUNK_FILES / pdf).read_bytes())}).json()


def best(label, parsed, pdf, expected_job):
    roles = upload(parsed, pdf)["roles"]
    top = roles["matches"][0]["jobId"] if roles and roles["matches"] else None
    check(f"{label}: best match is {expected_job}", top == expected_job and roles["status"] == "ok", (top, roles and roles["status"]))
    return roles


roles = best("Java engineer", JAVA, "r01_standard.pdf", "JOB-J1")
check("the best match shows its score, matched and missing skills and why", roles["matches"][0]["score"] > 60 and "Java" in " ".join(roles["matches"][0]["matchedSkills"]).title()
      and roles["matches"][0]["experience"] and set(roles["matches"][0]["parts"]) == {"skills", "text", "title", "experience", "degree", "location"})
log = [x for x in client.get("/api/log").json()["rows"] if x["step"] == "match_roles"]
check("matching is in the audit log with the record id and the top job ids only",
      log and log[0]["record_id"] == "CAN-031026-00042" and log[0]["details"]["top"][0]["jobId"] == "JOB-J1", log and log[0])
best("Sales manager", SALES, "r08_nontech_sales.pdf", "JOB-J2")
best("Lab analyst", CHEM, "r09_cv_word.pdf", "JOB-J8")

shown = {m["jobId"] for m in upload(JAVA)["roles"]["matches"]}
check("closed, expired and deleted jobs never show up", not ({"JOB-J4", "JOB-J5", "JOB-J6"} & shown), shown)
check(f"only the {OPEN_JOB_COUNT} open jobs are counted", upload(JAVA)["roles"]["open_jobs"] == OPEN_JOB_COUNT)

# a candidate who fits nothing
odd = variant(JAVA, technical_skills=["Pottery", "Glazing"], total_experience={"years": 1, "months": 0}, address="Delhi",
              workExperience=[{"jobTitle": "Potter", "companyName": "X", "startDate": "2025-01-01"}], education=[{"degree": "Diploma in Ceramics"}])
matching._cache["at"] = 0
res = asyncio.run(matching.match_roles(odd, "Potter. Pottery and glazing, ceramics studio, kiln firing, clay sculpture, Delhi."))
check("a candidate who fits nothing gets 'no strong match'", res["status"] == "no_strong_match", res["message"])

# ---------- the Screening step: the new profile goes onto its best 3 jobs ----------
state["screen_calls"].clear()
r = upload(JAVA)
calls = state["screen_calls"]
top3 = [m["id"] for m in r["roles"]["matches"]]
check("a strong match puts the new candidate on each of the top 3 jobs, by the long IDs", [c["jobId"] for c in calls] == top3 and len(calls) == 3 and {c["applicantId"] for c in calls} == {"u1"}, calls)
check("...at the SCREENING stage, status ONGOING", {(c["stages"], c["status"]) for c in calls} == {("SCREENING", "ONGOING")}, calls)
check("the result says so, for the whole run and for each job", r["roles"]["screening"]["status"] == "screened" and all(m["screening"]["status"] == "screened" for m in r["roles"]["matches"]), r["roles"]["screening"])
log = [x for x in client.get("/api/log").json()["rows"] if x["step"] == "screen_top_roles"]
check("it is in the audit log with the record id and the job ids only", log and log[0]["decision"] == "screened" and log[0]["record_id"] == "CAN-031026-00042"
      and [j["jobId"] for j in log[0]["details"]["jobs"]] == [m["jobId"] for m in r["roles"]["matches"]], log and log[0])

# some jobs refused: the profile stays created, the outcome says "partial", and a retry sends only the failed one
state["screen_calls"].clear()
state["screen"] = {top3[1]: 500}
r = upload(JAVA)
check("one job refused: partial, and the profile is still created", r["profile"]["status"] == "created" and r["roles"]["screening"]["status"] == "partial"
      and r["roles"]["matches"][1]["screening"]["status"] == "failed" and "HTTP 500" in r["roles"]["screening"]["message"], r["roles"]["screening"])
state["screen"], state["screen_calls"] = {}, []
again = asyncio.run(pipeline.screen_top_roles(audit.Run("x.pdf", b"x", "test"), r["profile"], r["roles"]))
check("a retry sends only the job that failed", [c["jobId"] for c in state["screen_calls"]] == [top3[1]] and again["status"] == "screened", (state["screen_calls"], again))

state["screen"] = {t: 409 for t in top3}
r = upload(JAVA)
check("'already on this job' counts as done", r["roles"]["screening"]["status"] == "screened" and r["roles"]["matches"][0]["screening"]["status"] == "already", r["roles"]["screening"])
state["screen"] = {t: 500 for t in top3}
r = upload(JAVA)
check("every job refused: failed, and the profile is still created", r["roles"]["screening"]["status"] == "failed" and r["profile"]["status"] == "created", r["roles"]["screening"])
state["screen"], state["screen_calls"] = {}, []

# nothing strong, no new profile, or no matching: nobody is put on a job
matching._cache["at"] = 0
res = asyncio.run(matching.match_roles(odd, "Potter. Pottery and glazing, ceramics studio, kiln firing, clay sculpture, Delhi."))
out = asyncio.run(pipeline.screen_top_roles(audit.Run("x.pdf", b"x", "test"), {"status": "created", "id": "u1", "applicantId": "C"}, res))
check("no strong match: nobody is put on a job, and it says why", out["status"] == "skipped" and state["screen_calls"] == [], (out, state["screen_calls"]))
dup = asyncio.run(pipeline.screen_top_roles(audit.Run("x.pdf", b"x", "test"), {"status": "duplicate"}, roles))
check("an existing (duplicate) profile is not put on any job", dup is None and state["screen_calls"] == [])
noid = asyncio.run(pipeline.screen_top_roles(audit.Run("x.pdf", b"x", "test"), {"status": "created", "applicantId": "C"}, roles))
check("a created profile with no ID is skipped, not guessed", noid["status"] == "skipped" and state["screen_calls"] == [], noid)
r = upload(variant(JAVA, email=""))
check("profile not created: nothing is sent to jobs", r["roles"] is None and state["screen_calls"] == [])
state["jobs_status"] = 500
r = upload(JAVA)
check("job list down: no matches, so nothing is sent to jobs", r["roles"]["status"] == "failed" and "screening" not in r["roles"] and state["screen_calls"] == [], r["roles"])
state["jobs_status"] = 200

# no profile -> no matching; the job list down -> the profile is still created
r = upload(variant(JAVA, email=""))
check("profile not created: matching is skipped", r["roles"] is None and r["profile"]["status"] == "not_created")
state["jobs_status"] = 500
r = upload(JAVA)
check("job list down: the profile is still created, matching says it failed", r["profile"]["status"] == "created" and r["roles"]["status"] == "failed", r["roles"])
state["jobs_status"] = 200
os.environ["QUESTLIGHT_TOKEN"] = ""
r = upload(JAVA)
check("no token: the profile is skipped, so there is no matching", r["profile"]["status"] == "skipped" and r["roles"] is None)
finish()
