"""The "already in Questlight?" check (GET /applicants/{email}): the lookup itself against a mock Questlight (found, not found,
deleted, every way it can fail), the early check on the email written in the resume (before the parse), the late check on the
parsed email, and what the agent's tools do with a candidate who was found. Nothing leaves this machine."""
import asyncio

import harness

harness.isolate(QUESTLIGHT_TOKEN="good-token")
from harness import check, finish, serve  # noqa: E402

from fastapi import FastAPI, Request  # noqa: E402
from fastapi.responses import JSONResponse, Response  # noqa: E402

mock = FastAPI()
seen = []          # (raw path, Authorization header) of every lookup
answer = {"mode": "none"}
RECORD = {"_id": "uuid-1", "applicantId": "CAN-010126-00001", "name": "Rohit Verma", "status": "SCREENING",
          "resume": {"file_name": "cv.pdf", "key": "k"}, "isDeleted": False}


@mock.get("/api/applicants/{email}")
async def by_email(email: str, request: Request):
    seen.append((request.scope["raw_path"].decode(), request.headers.get("authorization")))
    mode = answer["mode"]
    if mode == "found":
        return {"statusCode": 200, "data": dict(RECORD)}
    if mode == "deleted":
        return {"statusCode": 200, "data": {**RECORD, "isDeleted": True}}
    if mode == "503":
        return JSONResponse({"message": "down"}, 503)
    if mode == "404":
        return JSONResponse({"message": "not found"}, 404)
    if mode == "garbage":
        return Response("<html>oops</html>", media_type="text/html")
    if mode == "noid":
        return {"statusCode": 200, "data": {"name": "x"}}
    return {"statusCode": 200, "message": "Request successful", "data": None}


questlight_mock = serve(mock)
import os  # noqa: E402

os.environ["QUESTLIGHT_BASE_URL"] = questlight_mock.url + "/api"

from app.intake import questlight  # noqa: E402

real_find = questlight.find_by_email   # the harness below replaces it for the pipeline and agent checks


def lookup(mode, email="Rohit.Verma@Example.com"):
    answer["mode"] = mode
    return asyncio.run(real_find(email))


# ---- the lookup itself ----
r = lookup("none")
check("an unknown email: 200 with data null is 'not_found'", r["status"] == "not_found", r)
check("the email is sent URL-encoded with the bearer token", seen[-1] == ("/api/applicants/Rohit.Verma%40Example.com", "Bearer good-token"), seen[-1])
r = lookup("found")
check("a known email: found, with the code, status, name and whether a resume is on file",
      r["status"] == "found" and r["id"] == "uuid-1" and r["applicantId"] == "CAN-010126-00001" and r["name"] == "Rohit Verma"
      and r["candidate_status"] == "SCREENING" and r["has_resume"] is True and r["deleted"] is False, r)
check("a record deleted in Questlight is found, marked deleted", lookup("deleted")["deleted"] is True)
check("HTTP 503 is an error, not 'not found'", lookup("503")["status"] == "error")
check("HTTP 404 is an error too (can't tell a missing route from a missing person)", lookup("404")["status"] == "error")
check("an answer that isn't JSON is an error", lookup("garbage")["status"] == "error")
check("a record with no id is an error", lookup("noid")["status"] == "error")
questlight_url = os.environ["QUESTLIGHT_BASE_URL"]
os.environ["QUESTLIGHT_BASE_URL"] = "http://127.0.0.1:9/api"   # nothing listens there
check("Questlight unreachable is an error", asyncio.run(real_find("a@b.co"))["status"] == "error")
os.environ["QUESTLIGHT_BASE_URL"] = questlight_url
token = os.environ.pop("QUESTLIGHT_TOKEN")
before = len(seen)
check("no token: skipped, and nothing is sent", asyncio.run(real_find("a@b.co"))["status"] == "skipped" and len(seen) == before)
os.environ["QUESTLIGHT_TOKEN"] = token

# ---- finding the email and the name in a document's text ----
check("emails are found, lower-cased, once each, in order",
      questlight.emails_in("Mail: Priya.S@Gmail.com | ref: x@y.co.in. again PRIYA.S@gmail.com") == ["priya.s@gmail.com", "x@y.co.in"])
check("no email in the text: an empty list", questlight.emails_in("no contact here, just words") == [])
check("the name must show up (first and last word), whatever the case or middle names",
      questlight.name_in_text("Priya Sharma", "PRIYA K. SHARMA, Pune") and not questlight.name_in_text("Priya Sharma", "Rohit Verma")
      and not questlight.name_in_text("Priya Sharma", "Priya Nair") and not questlight.name_in_text("", "Priya"))

# ---- the pipeline and the agent, with the lookup faked per test ----
import agent_harness as ah  # noqa: E402
from fixtures.parsed import ROHIT  # noqa: E402

fakes = ah.start({"rohit": ROHIT, "stray": ROHIT})
EMAIL = ROHIT["email"]                      # the email written in the fixture resume, which the parser also returns

from app import pipeline  # noqa: E402
from app.agent import intakes  # noqa: E402
from harness import RESUMES  # noqa: E402

DATA = (RESUMES / "Rohit_Verma_Python_Backend.pdf").read_bytes()


def run(name="Rohit_Verma.pdf"):
    fakes.parse_calls.clear(), fakes.creates.clear(), fakes.lookups.clear()
    state = {}
    return asyncio.run(pipeline.process_file(name, DATA, state=state)), state


# early check: the email is in the resume text, the person exists and their name is in the text too
fakes.existing[EMAIL] = {}
r, state = run()
p = r["profile"]
check("found early: a finished 'duplicate', the resume is never parsed and nothing is created",
      r["ok"] and r["decision"] == "accepted" and p["status"] == "duplicate" and not fakes.parse_calls and not fakes.creates, (p, fakes.parse_calls))
check("...it names the existing profile: code, Questlight status, resume on file, not deleted",
      p["applicantId"] == "CAN-EXIST-1" and p["existing"] == {"name": "Rohit Verma", "candidate_status": "SCREENING", "has_resume": True, "deleted": False}
      and "already in Questlight" in p["message"], p)
check("...no job matching, no parsed data, and the email was asked about once", r["roles"] is None and r["redacted"] is None and fakes.lookups == [EMAIL], fakes.lookups)
check("...the run's state holds the file with nothing parsed, so the agent can follow up", state["parsed"] is None and state["incomplete"] is None and "run" in state)
check("the lookup and its answer are in the trace, with no email in it",
      any(x["name"] == "check applicant exists" for x in r["trace"]["spans"]) and EMAIL not in str(r["trace"]))

# not found: the resume goes on as usual, and the same email is not asked about again after the parse
fakes.existing.clear()
r, _ = run()
check("not found: parsed and created as usual, the email asked about only once",
      r["profile"]["status"] == "created" and fakes.parse_calls and len(fakes.creates) == 1 and fakes.lookups == [EMAIL], (r["profile"], fakes.lookups))

# found, but the name isn't in the resume: a stray email, so the resume is read, and the parsed email is checked again
fakes.existing[EMAIL] = {"name": "Meera Iyer"}
r, state = run()
check("found by email but another name: the resume is read, then the parsed email decides (late check)",
      fakes.parse_calls and fakes.lookups == [EMAIL, EMAIL] and r["profile"]["status"] == "duplicate" and not fakes.creates, (fakes.lookups, r["profile"]))
check("...the late answer keeps what was read (the agent can match roles for it)", r["redacted"] and state["parsed"] is not None)

# deleted in Questlight
fakes.existing[EMAIL] = {"deleted": True}
r, _ = run()
check("found but deleted: says so, nothing is created, nothing is parsed",
      r["profile"]["status"] == "duplicate" and r["profile"]["existing"]["deleted"] is True and "deleted" in r["profile"]["message"]
      and not fakes.creates and not fakes.parse_calls, r["profile"])

# the check itself failing never blocks an intake
fakes.existing.clear()
fakes.lookup_mode = "error"
r, _ = run()
check("the check fails: the intake carries on, the 409 on create stays the safety net",
      r["ok"] and r["profile"]["status"] == "created" and fakes.parse_calls and len(fakes.creates) == 1, r["profile"])
check("...the failure is in the trace as a failed step",
      any(x["name"] == "check applicant exists" and x["status"] == "error" for x in r["trace"]["spans"]), [x["name"] for x in r["trace"]["spans"]])
fakes.lookup_mode = "ok"

# ---- the agent's tools ----
fakes.existing[EMAIL] = {}


async def agent():
    out = {}
    async with ah.session(fakes) as s:
        call = lambda tool, **a: ah.call(s, tool, **a)  # noqa: E731
        fakes.parse_calls.clear(), fakes.creates.clear(), fakes.lookups.clear()
        out["first"] = await call("process_resume", file_url="https://files.test/rohit.pdf", file_name="Rohit_Verma.pdf")
        out["lookups_after_first"] = list(fakes.lookups)
        out["again"] = await call("process_resume", file_url="https://files.test/rohit.pdf", file_name="Rohit_Verma.pdf")
        out["lookups_after_again"] = list(fakes.lookups)
        fid = out["first"]["file_id"]
        out["create"] = await call("create_profile", file_id=fid)
        out["match"] = await call("match_roles", file_id=fid)
        out["details"] = await call("provide_missing_details", file_id=fid, answers=[])
    return out


o = asyncio.run(agent())
f = o["first"]
check("process_resume on an existing candidate: duplicate, with who they are, no candidate summary, nothing created",
      f["ok"] and f["profile"]["status"] == "duplicate" and f["profile"]["existing"]["has_resume"] is True
      and f["profile_created"] is False and f["candidate"] is None and not fakes.creates and not fakes.parse_calls, f)
check("...the agent is told what to say, and gets a file_id", "already in Questlight" in f["next"] and f["file_id"], f)
# a plain https link (not the chat page's upload link) isn't remembered between calls, so asking again repeats only the 1 s check
check("asking again for the same file gives the same answer and still reads or creates nothing",
      o["again"]["profile"]["status"] == "duplicate" and o["lookups_after_again"] == [EMAIL, EMAIL] and not fakes.parse_calls and not fakes.creates)
check("create_profile on it: the duplicate as found, nothing sent to Questlight",
      o["create"]["ok"] and o["create"]["profile_created"] is False and o["create"]["profile"]["status"] == "duplicate" and not o["create"]["missing_required"] and not fakes.creates, o["create"])
check("match_roles on it explains there is nothing to match, instead of failing", o["match"]["ok"] is False and "already in Questlight" in o["match"]["error"], o["match"])
check("provide_missing_details on it: refused, the profile exists", o["details"]["ok"] is False and "already exists" in o["details"]["error"], o["details"])
import sqlite3  # noqa: E402

conn = sqlite3.connect(os.environ["AUDIT_DB"])
steps = [row[0] for row in conn.execute("SELECT step FROM audit_log")]
dump = chr(10).join(conn.iterdump())
conn.close()
check("the audit log has the check, without the email", "check_exists" in steps and EMAIL not in dump, steps)
finish()
