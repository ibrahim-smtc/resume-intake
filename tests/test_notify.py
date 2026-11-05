"""After a recruiter submits the fill-in form, the outcome is sent to them on WhatsApp (app/agent/notify.py): Perfox forwards the
recruiter's number as the X-Sa-End-User-Phone header on the tool call, process_resume keeps it with the file, and the form's
submit sends the result through Perfox's POST /messages. Perfox and Questlight are mock servers here; nothing real is messaged."""
import asyncio
import os
import sqlite3

import harness

harness.isolate(PUBLIC_BASE_URL="https://intake.example")
from harness import check, finish, serve  # noqa: E402

from fastapi import FastAPI, Request  # noqa: E402
from fastapi.responses import JSONResponse  # noqa: E402

perfox = FastAPI()
sent = []
mode = {"status": 201}


@perfox.post("/api/v1/messages")
async def messages(request: Request):
    sent.append({"auth": request.headers.get("authorization"), "body": await request.json()})
    if mode["status"] != 201:
        return JSONResponse({"error": "destination_not_allowed"}, mode["status"])
    return JSONResponse({"id": "msg1", "status": "sent"}, 201)


perfox_mock = serve(perfox)

import agent_harness as ah  # noqa: E402
from fixtures.parsed import ROHIT, variant  # noqa: E402

# a candidate who matches the open jobs but whose two job titles the resume leaves out, so the form is needed
NOTITLES = variant(ROHIT, workExperience=[{**w, "jobTitle": ""} for w in ROHIT["workExperience"]])

fakes = ah.start({stem: NOTITLES for stem in ("onwa", "nophone", "badphone", "refused", "nokey", "dupe")})
RECRUITER = "+919000000001"
TITLES = {"job_title:0": "Backend Engineer", "job_title:1": "Python Developer"}

import httpx  # noqa: E402

from app import settings  # noqa: E402
from app.intake import questlight  # noqa: E402

_create = questlight.create_profile


async def create_with_id(*args, **kwargs):   # the harness's stub returns no ID, and screening needs one
    result = await _create(*args, **kwargs)
    return {**result, "id": "uuid-new-1"} if result["status"] == "created" else result


async def fake_screening(applicant_id, job_id, status="NEW_CANDIDATE"):
    return {"status": "screened", "message": "added to the job at the Screening stage", "status_updated": True}


questlight.create_profile = create_with_id
questlight.add_to_screening = fake_screening
from app.agent import notify  # noqa: E402

settings.PERFOX_BASE_URL = perfox_mock.url + "/api/v1"
settings.PERFOX_MESSAGING_KEY = "sk_test_messaging_key"
settings.PERFOX_WHATSAPP_FROM = "+918065492151"

web = httpx.Client(base_url=fakes.server.url, timeout=60)


async def take_in(stem, phone=None):
    kept = fakes.headers
    fakes.headers = {**kept, **({"X-Sa-End-User-Phone": phone} if phone is not None else {})}   # only for this call
    try:
        async with ah.session(fakes) as s:
            return await ah.call(s, "process_resume", file_url=f"https://files.test/{stem}.pdf")
    finally:
        fakes.headers = kept


def submit(res):
    tok = res["form_url"].rsplit("/", 1)[-1]
    return web.post(f"/api/form/{tok}", json={"values": TITLES, "skip": False})


def audit_steps():
    conn = sqlite3.connect(os.environ["AUDIT_DB"])
    rows = conn.execute("SELECT step, decision, reason FROM audit_log WHERE step = 'notify_whatsapp'").fetchall()
    dump = "\n".join(conn.iterdump())
    conn.close()
    return rows, dump


# ---- the number reaches the file, the form's submit sends the outcome ----
a = asyncio.run(take_in("onwa", RECRUITER))
check("a file taken in over WhatsApp has a form", "form_url" in a and not sent, a.get("profile"))
resp = submit(a)
check("submitting the form still answers with the finished profile", resp.status_code == 200 and resp.json()["status"] == "done", resp.text[:200])
check("exactly one WhatsApp message went out, to the recruiter, from the agent's number, as plain text, with the messaging key",
      len(sent) == 1 and sent[0]["body"]["to"] == RECRUITER and sent[0]["body"]["from"] == "+918065492151"
      and sent[0]["body"]["channel"] == "whatsapp" and set(sent[0]["body"]) == {"to", "from", "channel", "text"}
      and sent[0]["auth"] == "Bearer sk_test_messaging_key", sent)
text = sent[0]["body"]["text"]
check("the text says the profile was created, with the name and the Questlight code", "Profile created" in text and "Rohit" in text and "CAN-STUB-" in text, text)
check("...and the jobs the candidate was screened for, with scores", "Added to Screening for:" in text and "/100)" in text, text)
check("...and never the candidate's email or phone", "@" not in text and "9" * 5 not in text.replace(RECRUITER, "") , text)
check("a second submit of the same form sends nothing more", submit(a).status_code in (200, 410) and len(sent) == 1, len(sent))

# ---- every way it can not go out leaves the form working ----
before = len(sent)
b = asyncio.run(take_in("nophone"))   # no X-Sa-End-User-Phone (web chat, say)
check("no number forwarded: the form works and nothing is sent", submit(b).json()["status"] == "done" and len(sent) == before)
c = asyncio.run(take_in("badphone", "not-a-number"))
check("a header that isn't a phone number is ignored", submit(c).json()["status"] == "done" and len(sent) == before)

mode["status"] = 422
d = asyncio.run(take_in("refused", RECRUITER))
resp = submit(d)
check("Perfox refuses the message: the recruiter's form still ends as created", resp.status_code == 200 and resp.json()["status"] == "done" and len(sent) == before + 1)
mode["status"] = 201

settings.PERFOX_MESSAGING_KEY = ""
e = asyncio.run(take_in("nokey", RECRUITER))
check("no messaging key set: nothing is sent, the form works", submit(e).json()["status"] == "done" and len(sent) == before + 1)
settings.PERFOX_API_KEY = "sk_test_api_key"          # no separate messaging key: the resume key is used
g = asyncio.run(take_in("nokey", RECRUITER))
submit(g)
check("no messaging key but the API key is set: it sends with the API key", len(sent) == before + 2 and sent[-1]["auth"] == "Bearer sk_test_api_key", sent[-1]["auth"] if sent else None)
settings.PERFOX_API_KEY = ""
settings.PERFOX_MESSAGING_KEY = "sk_test_messaging_key"

# ---- an existing candidate gets the "already exists" wording ----
fakes.create_mode = "duplicate"
f = asyncio.run(take_in("dupe", RECRUITER))
submit(f)
check("a duplicate outcome is worded as already existing, with no roles", len(sent) == before + 3 and "already in Questlight" in sent[-1]["body"]["text"]
      and "Screening" not in sent[-1]["body"]["text"], sent[-1]["body"]["text"] if len(sent) > before + 2 else sent)
fakes.create_mode = "ok"

# ---- the audit log: outcomes only, never the number ----
rows, dump = audit_steps()
outcomes = sorted(r[1] for r in rows)
check("the audit log records sent, failed and skipped, by outcome", outcomes.count("sent") == 3 and "failed" in outcomes and outcomes.count("skipped") >= 2, outcomes)
check("the recruiter's number is nowhere in the audit log", RECRUITER not in dump and RECRUITER.lstrip("+") not in dump)
check("the message text builder never raises on an empty file", isinstance(notify.message_for(type("I", (), {"profile": None, "parsed": None, "roles": None})()), str))
finish()
