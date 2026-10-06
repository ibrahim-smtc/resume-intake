"""The "Load into Questlight" step against a mock Questlight: what is sent, what blocks a profile, and every way the call can
fail. The parser is stubbed, masking is on (this suite also checks the redaction); nothing leaves this machine."""
import copy
import json
import os

import harness

harness.isolate(QUESTLIGHT_TOKEN="good-token", RESUME_PARSER="perfox", MASKING_ENABLED="true")
from harness import JUNK_FILES, check, finish, serve  # noqa: E402

from fastapi import FastAPI, Request  # noqa: E402
from fastapi.responses import JSONResponse  # noqa: E402

mock = FastAPI()
calls = []


@mock.post("/api/applicants/create-applicant")
async def create(request: Request):
    if request.headers.get("authorization") != "Bearer good-token":
        return JSONResponse({"statusCode": 401, "message": "Unauthorized"}, 401)
    form = await request.form()
    d = json.loads(form["applicantData"])
    calls.append({"data": d, "has_resume": "resume" in form, "ctype": request.headers["content-type"][:19]})
    for f in ("name", "email", "skills", "workExperience", "education"):
        if f not in d or d[f] in ("", None):
            return JSONResponse({"statusCode": 400, "message": f"{f} should not be empty"}, 400)
    if d["email"] == "dup@example.com":
        return JSONResponse({"statusCode": 409, "message": "Applicant already exists"}, 409)
    return JSONResponse({"statusCode": 201, "message": "The Profile was created successfully.",
                         "data": {"_id": "uuid-1", "applicantId": "CAN-031026-00001", **d}}, 201)


questlight = serve(mock)
QUESTLIGHT_API = questlight.url + "/api"
os.environ["QUESTLIGHT_BASE_URL"] = QUESTLIGHT_API

from fastapi.testclient import TestClient  # noqa: E402

from app.intake import parser_perfox  # noqa: E402
from app.main import app  # noqa: E402
from fixtures.parsed import PRIYA, variant  # noqa: E402

pdf = (JUNK_FILES / "r01_standard.pdf").read_bytes()
client = TestClient(app)
current = {}


async def stub(name, data, ext):
    return copy.deepcopy(current["parsed"]), None


parser_perfox.parse_resume = stub


def run(label, parsed, status, api_calls, **expect):
    current["parsed"] = parsed
    before = len(calls)
    r = client.post("/api/process", files={"file": ("r01_standard.pdf", pdf)}).json()
    made = len(calls) - before
    ok = r["profile"]["status"] == status and made == api_calls and all(r[k] == v for k, v in expect.items())
    check(f"{label}: status={r['profile']['status']}, Questlight calls={made}", ok, (r["profile"], r["missing_fields"], r["missing_recommended"]))
    return r


# ---- a complete resume: what is sent ----
r = run("full resume", PRIYA, "created", 1, missing_fields=[], missing_recommended=[])
sent = calls[-1]["data"]
check("the resume file is attached, as multipart", calls[-1]["has_resume"] and calls[-1]["ctype"] == "multipart/form-data")
check("only fields Questlight has are sent (no github, date of birth or empty portfolio)", not ({"github", "dateOfBirth", "portfolio"} & sent.keys()))
check("skills are merged and de-duplicated", sent["skills"] == ["Java", "Python", "Leadership"], sent["skills"])
check("empty optional values are dropped (a job's location, an unset end date)",
      "location" not in sent["workExperience"][0] and "endDate" not in sent["workExperience"][1])
check("GitHub has no field in Questlight, so it goes in the notes", "GitHub: https://github.com/priya" in sent["additionalNotes"])
check("the notes say how it came in (the page's upload)", "Source: resume intake, manual upload" in sent["additionalNotes"], sent["additionalNotes"])
check("the real email and phone are NOT in what the page gets back (masking on)",
      "priya.nair@example.com" not in json.dumps(r) and "98765" not in json.dumps(r) and r["redacted"]["email"] == "[REDACTED]")
check("the id Questlight returned is passed on", r["profile"]["applicantId"] == "CAN-031026-00001")

# ---- what blocks a profile: nothing is sent ----
run("no email", variant(PRIYA, email=""), "not_created", 0, missing_fields=["email"])
run("bad email", variant(PRIYA, email="priya at example"), "not_created", 0, missing_fields=["email"])
run("no name", variant(PRIYA, name=" "), "not_created", 0, missing_fields=["name"])
run("no skills", variant(PRIYA, technical_skills=[], soft_skills=[]), "not_created", 0, missing_fields=["skills"])
run("no education", variant(PRIYA, education=[]), "not_created", 0, missing_fields=["education"])
run("jobs without start dates only", variant(PRIYA, workExperience=[{"jobTitle": "Dev", "companyName": "X", "startDate": ""}]),
    "not_created", 0, missing_fields=["workExperience"])
run("several missing at once", variant(PRIYA, email="", technical_skills=[], soft_skills=[], education=[]), "not_created", 0,
    missing_fields=["email", "skills", "education"])
r = run("a job without a title", variant(PRIYA, workExperience=[{"companyName": "Acme", "startDate": "2020-01-01"}]), "not_created", 0)
check("...is reported by name, so the recruiter can be asked", r["missing_fields"] == ["a job title for the job at Acme"]
      and r["profile"]["missing_items"][0]["id"] == "job_title:0", r["profile"])

# ---- fine to create ----
run("no phone (only recommended)", variant(PRIYA, phoneNumber=""), "created", 1, missing_fields=[], missing_recommended=["phoneNumber"])
run("duplicate email", variant(PRIYA, email="dup@example.com"), "duplicate", 1)

# ---- settings and failures ----
os.environ["QUESTLIGHT_ATTACH_RESUME"] = "false"
run("QUESTLIGHT_ATTACH_RESUME=false", PRIYA, "created", 1)
check("...sends no file", calls[-1]["has_resume"] is False)
os.environ.pop("QUESTLIGHT_ATTACH_RESUME")

os.environ["QUESTLIGHT_TOKEN"] = "expired"
r = run("rejected token", PRIYA, "failed", 0)
check("...tells you to get a new token", "token" in r["profile"]["message"])
os.environ["QUESTLIGHT_TOKEN"] = ""
r = run("no token set", PRIYA, "skipped", 0)
check("...says the token is not set", "QUESTLIGHT_TOKEN is not set" in r["profile"]["message"])
os.environ["QUESTLIGHT_TOKEN"] = "good-token"
os.environ["QUESTLIGHT_BASE_URL"] = "http://127.0.0.1:9/api"
run("Questlight unreachable", PRIYA, "failed", 0)
os.environ["QUESTLIGHT_BASE_URL"] = QUESTLIGHT_API

# Questlight's real 400 body hides its reasons ("[object Object]; ..."); they are in errorMessage.response.errors
from app.intake import questlight  # noqa: E402


class Resp:
    status_code = 400

    @staticmethod
    def json():
        return {"statusCode": 400, "errorMessage": {"response": {"errors": [
            {"field": "jobTitle", "message": "jobTitle should not be empty"}, {"field": "jobTitle", "message": "jobTitle should not be empty"},
            {"field": "email", "message": "email must be an email"}]}}, "message": "[object Object]; [object Object]; [object Object]"}


check("Questlight's 400 is reported with its real reasons, once each",
      questlight._problem(Resp()) == "jobTitle should not be empty; email must be an email", questlight._problem(Resp()))
finish()
