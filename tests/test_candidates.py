"""The other direction, job -> candidates: loading Questlight's candidate list (paged, cached, stripped of contact details),
ranking candidates for a job, leaving out the hired and onboarding, finding the job from an ID or title, and the agent's two
tools (find_candidates_for_job reads, add_candidates_to_job writes). Questlight is a mock server; nothing real is touched."""
import asyncio
import json
import math
import os
import sqlite3

import harness

harness.isolate()
from harness import check, finish, serve  # noqa: E402

from fastapi import FastAPI, Request  # noqa: E402
from fastapi.responses import JSONResponse  # noqa: E402

from fixtures.candidates import ALL, PEOPLE  # noqa: E402
from fixtures.jobs import JOBS  # noqa: E402

state = {"find_status": 200, "bad_page": None, "find_calls": [], "screen": {}, "screen_calls": []}
mock = FastAPI()


@mock.get("/api/applicants/findAll")
async def find_all(request: Request):
    q = dict(request.query_params)
    state["find_calls"].append(q)
    page, limit = int(q["page"]), int(q["limit"])
    if limit > 100:
        return JSONResponse({"message": "limit too big"}, 400)
    if state["find_status"] != 200 or state["bad_page"] == page:
        return JSONResponse({"message": "boom"}, state["find_status"] if state["find_status"] != 200 else 500)
    rows = ALL[(page - 1) * limit: page * limit]
    return {"statusCode": 200, "data": {"data": rows, "total": len(ALL), "totalPages": math.ceil(len(ALL) / limit), "currentPage": page}}


@mock.post("/api/jobs/applicantMatching/create")
async def screen(body: dict):
    state["screen_calls"].append(body)
    code = state["screen"].get(body["applicantId"], 201)
    return JSONResponse({"message": "no"} if code >= 400 else {"data": {}}, code)


questlight_server = serve(mock)
os.environ["QUESTLIGHT_BASE_URL"] = questlight_server.url + "/api"

import agent_harness as ah  # noqa: E402

os.environ["QUESTLIGHT_TOKEN"] = "good-token"
fakes = ah.start({})

from app.intake import candidates, matching  # noqa: E402
from app.observability import audit, tracing  # noqa: E402

J1 = next(j for j in JOBS if j["_id"] == "J1")   # Java Backend Developer
J3 = next(j for j in JOBS if j["_id"] == "J3")   # Data Scientist: nobody in the fixtures has its skills
CACHED, LEFT_OUT = len(ALL) - 1, 3               # the deleted one is dropped on load; placed, onboarding and ready are left out of ranking


def fresh():
    candidates._cache["at"] = 0
    state["find_calls"].clear()


def run(coro):
    return asyncio.run(coro)


# ---------- loading ----------
fresh()
people, err = run(candidates.fetch_candidates())
pages = sorted(int(c["page"]) for c in state["find_calls"])
check("every page is fetched, 100 at a time, with the same filters as Questlight's own page", err is None and pages == [1, 2]
      and {c["limit"] for c in state["find_calls"]} == {"100"} and state["find_calls"][0]["columnFilters"] == "%5B%5D", state["find_calls"])
check("a deleted candidate is dropped on load", len(people) == CACHED and "c-gone" not in {p["id"] for p in people}, len(people))
dump = json.dumps(people)
check("no contact details, birth date or password-style field survive the loader", all(x not in dump for x in ("@example.com", "+91-", "1990-01-01", "otp", "notarealhash")))
people_again, _ = run(candidates.fetch_candidates())
check("the second look uses the cache: no new requests", len(state["find_calls"]) == 2 and people_again is people)

# ---------- ranking ----------
res = run(candidates.find_for_job(J1, 10))
ids = [c["id"] for c in res["candidates"]]
check("the Java job's best candidate is the Java developer, with a strong score", res["status"] == "ok" and ids[0] == "c-asha" and res["candidates"][0]["score"] >= 70, res["candidates"][:1])
check("hired, onboarding, ready-for-onboarding and deleted candidates never appear", not ({"c-placed", "c-onb", "c-ready", "c-gone"} & set(ids)), ids)
check("they are counted as left out, the rest as considered", res["left_out"] == LEFT_OUT and res["considered"] == CACHED - LEFT_OUT, (res["left_out"], res["considered"]))
check("results come best first, with the reasons", [c["score"] for c in res["candidates"]] == sorted((c["score"] for c in res["candidates"]), reverse=True)
      and "Java" in " ".join(res["candidates"][0]["matchedSkills"]).title() and res["candidates"][0]["experience"])
check("the message holds no candidate name (it goes to the audit log)", not any(p["name"] in res["message"] for p in PEOPLE), res["message"])
check("top_k is clamped to 1..10", len(run(candidates.find_for_job(J1, 0))["candidates"]) == 1 and len(run(candidates.find_for_job(J1, 99))["candidates"]) == 10)
weak = run(candidates.find_for_job(J3, 3))
check("a job nobody fits gets 'no strong candidate'", weak["status"] == "no_strong_match" and weak["candidates"][0]["score"] < matching.WEAK_BELOW, weak["message"])

# ---------- finding the job ----------
open_jobs = [j for j in JOBS if j["_id"] in ("J1", "J2", "J3", "J7", "J8")]
check("a job is found by its job ID or its long ID, in any case", matching.find_job("JOB-J1", open_jobs)[0]["_id"] == "J1" and matching.find_job("j1", open_jobs)[0]["_id"] == "J1")
check("...or by its exact title, or a unique part of it", matching.find_job("Data Scientist", open_jobs)[0]["_id"] == "J3" and matching.find_job("laboratory analyst", open_jobs)[0]["_id"] == "J8")
job, choices = matching.find_job("developer", open_jobs)
check("a title that fits several jobs returns the choices instead of guessing", job is None and {j["_id"] for j in choices} == {"J1", "J7"}, choices)
check("nothing fits: no job and no choices", matching.find_job("astronaut", open_jobs) == (None, []) and matching.find_job("", open_jobs) == (None, []))
check("a word inside another word is not a match ('qa' is not 'aqua')", matching.find_job("qa", [{"_id": "x", "jobId": "JOB-X", "jobPositionTitle": "Aquaculture Officer"}]) == (None, []))


# ---------- the agent's tools, over real HTTP with the real MCP client ----------
async def main():
    out = {}
    async with ah.session(fakes) as s:
        call = lambda tool, **a: ah.call(s, tool, **a)  # noqa: E731
        fresh()
        out["find"] = await call("find_candidates_for_job", job="JOB-J1")
        out["find2"] = await call("find_candidates_for_job", job="Java Backend Developer", top_k=2)
        out["title"] = await call("find_candidates_for_job", job="developer")
        out["nojob"] = await call("find_candidates_for_job", job="astronaut")
        out["closed"] = await call("find_candidates_for_job", job="JOB-J4")

        out["add"] = await call("add_candidates_to_job", job="JOB-J1", candidate_ids=["CAN-T-001", "can-t-004", "CAN-T-001"])
        out["add_calls"] = list(state["screen_calls"])
        state["screen_calls"].clear()
        out["add_bad"] = await call("add_candidates_to_job", job="JOB-J1", candidate_ids=["CAN-T-002", "CAN-T-007", "CAN-NOPE"])
        out["bad_calls"] = list(state["screen_calls"])
        state["screen"] = {"c-lena": 500}
        out["partial"] = await call("add_candidates_to_job", job="JOB-J1", candidate_ids=["CAN-T-001", "CAN-T-005"])
        state["screen"] = {}
        out["empty"] = await call("add_candidates_to_job", job="JOB-J1", candidate_ids=[])
        out["many"] = await call("add_candidates_to_job", job="JOB-J1", candidate_ids=[f"CAN-F-{i:03d}" for i in range(11)])
        out["ambiguous_add"] = await call("add_candidates_to_job", job="developer", candidate_ids=["CAN-T-001"])

        out["summary"] = await call("get_intake_summary", days=1)
        out["recent"] = await call("list_recent_intakes", limit=10)

        # Questlight's candidate list failing
        state["find_status"] = 500
        fresh()
        out["down"] = await call("find_candidates_for_job", job="JOB-J1")
        out["down_add"] = await call("add_candidates_to_job", job="JOB-J1", candidate_ids=["CAN-T-001"])
        state["find_status"], state["bad_page"] = 200, 2
        out["page2"] = await call("find_candidates_for_job", job="JOB-J1")
        state["bad_page"] = None
        out["recovered"] = await call("find_candidates_for_job", job="JOB-J1")
        os.environ["QUESTLIGHT_TOKEN"] = ""
        fresh()
        out["no_token"] = await call("find_candidates_for_job", job="JOB-J1")
        os.environ["QUESTLIGHT_TOKEN"] = "good-token"
    return out


o = asyncio.run(main())
f = o["find"]
check("find_candidates_for_job returns the job, the best candidates and the counts", f["ok"] and f["job"]["job_id"] == "JOB-J1" and f["candidates"][0]["candidate_id"] == "CAN-T-001"
      and f["considered"] == CACHED - LEFT_OUT and f["left_out_hired_or_onboarding"] == LEFT_OUT and len(f["candidates"]) == 3, f)
check("...in the plain words the agent reports: name, score, status, matched skills", {"name", "score", "status", "matched_skills", "missing_skills", "experience"} <= set(f["candidates"][0]))
check("...with the 'can't see who is already on the job' warning and the confirm-first hint", "already" in f["note"] and "ask the recruiter" in f["next"])
check("no contact details or internal fields reach the agent", all(x not in json.dumps(f) for x in ("@example.com", "+91-", "otp", "1990-01-01", "c-asha")), f)
check("a job title works too, and top_k limits the list", o["find2"]["ok"] and len(o["find2"]["candidates"]) == 2)
check("an ambiguous title asks which job instead of guessing", o["title"]["ok"] and o["title"]["needs_choice"] and {j["job_id"] for j in o["title"]["jobs"]} == {"JOB-J1", "JOB-J7"}, o["title"])
check("an unknown job is a clear error", o["nojob"]["ok"] is False and "list_open_roles" in o["nojob"]["error"])
check("a closed job can't be used (only open jobs are)", o["closed"]["ok"] is False)

check("add_candidates_to_job puts each chosen candidate on the job, by the long IDs, once", [(c["applicantId"], c["jobId"], c["stages"], c["status"]) for c in o["add_calls"]]
      == [("c-asha", "J1", "SCREENING", "ONGOING"), ("c-ravi", "J1", "SCREENING", "ONGOING")], o["add_calls"])
check("...and reports each one", o["add"]["ok"] and o["add"]["status"] == "screened" and [r["candidate"] for r in o["add"]["results"]] == ["CAN-T-001", "CAN-T-004"], o["add"])
bad = {r["candidate"]: r["status"] for r in o["add_bad"]["results"]}
check("hired and onboarding candidates are refused, unknown ones not found, and nothing is sent for them", bad == {"CAN-T-002": "refused", "CAN-T-007": "refused", "CAN-NOPE": "not_found"}
      and o["bad_calls"] == [] and o["add_bad"]["status"] == "failed", o["add_bad"])
check("one candidate failing makes the result 'partial', the other still goes through", o["partial"]["status"] == "partial"
      and {r["candidate"]: r["status"] for r in o["partial"]["results"]} == {"CAN-T-001": "screened", "CAN-T-005": "failed"}, o["partial"])
check("no IDs, too many IDs, or an ambiguous job are refused before anything is sent", o["empty"]["ok"] is False and o["many"]["ok"] is False and "at most" in o["many"]["error"]
      and o["ambiguous_add"].get("needs_choice") is True)

rows = audit.recent(500)
logged = {r["step"]: r for r in rows if r["step"] in audit.JOB_STEPS}
check("finding and adding are in the audit log with the job ID, and only candidate IDs (no names)", {"find_candidates", "add_to_screening"} <= set(logged)
      and logged["find_candidates"]["record_id"] == "JOB-J1" and "CAN-T-001" in json.dumps(logged["find_candidates"]["details"])
      and not any(p["name"] in json.dumps(r) for r in rows for p in PEOPLE), logged)
check("job-side runs are not counted as files received, and don't clutter the recent uploads", o["summary"]["files_received"] == 0 and o["recent"]["intakes"] == [], (o["summary"], o["recent"]))
check("the summary counts what recruiters added to jobs", sum(o["summary"]["added_to_jobs"].values()) >= 3, o["summary"]["added_to_jobs"])
trace = tracing.get_trace(f["trace_id"])
names = [s["name"] for s in trace["spans"]]
check("a find is one trace: loading the list and scoring are its steps", names[0] == "find_candidates_for_job" and "fetch candidates" in names and "score candidates" in names, names)

check("Questlight's candidate list failing is reported, not crashed on", o["down"]["ok"] is False and "HTTP 500" in o["down"]["error"] and o["down_add"]["ok"] is False)
check("one bad page fails the whole list (no silently missing candidates), and the next try recovers", o["page2"]["ok"] is False and o["recovered"]["ok"] is True, o["page2"])
check("no token: a clear message", o["no_token"]["ok"] is False and "QUESTLIGHT_TOKEN" in o["no_token"]["error"], o["no_token"])

conn = sqlite3.connect(os.environ["AUDIT_DB"])
stored = "\n".join(conn.iterdump())
check("no candidate name or contact detail is in the audit database", not any(x in stored for x in ("Asha", "Ravi", "@example.com", "+91-", "otp")))
finish()
