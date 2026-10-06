"""OPT-IN: checks the code against the REAL Questlight dev services. Skipped unless RUN_LIVE=1. Reads your real .env.

What it does, and what it never does:
- reads a fake resume with Questlight's real parser (a stateless call),
- reads the real open-jobs list (read-only),
- sends profiles that Questlight MUST reject (an invalid email is always included), to see which rules it enforces. The rejection
  is the point: nothing is ever created.
Run it after changing app/intake/questlight.py, to be sure the profile builder still satisfies Questlight's validator.
"""
import asyncio
import json
import os
import sys
import tempfile
from pathlib import Path

if os.getenv("RUN_LIVE") != "1":
    print("skipped: set RUN_LIVE=1 to run the live checks against Questlight's dev services")
    sys.exit(0)

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
os.environ["AUDIT_DB"] = str(Path(tempfile.mkdtemp()) / "audit.db")   # the real .env is read, but never the real audit log

import httpx  # noqa: E402

from harness import RESUMES, check, finish  # noqa: E402

from app import settings  # noqa: E402
from app.intake import matching, parser_questlight, questlight  # noqa: E402

headers = questlight.auth_headers()
check("QUESTLIGHT_TOKEN is set (needed for the checks below)", headers is not None)


async def parse_fake_resume():
    async with httpx.AsyncClient(timeout=60) as client:
        return await parser_questlight.parse(client, "Rohit_Verma_Python_Backend.pdf", (RESUMES / "Rohit_Verma_Python_Backend.pdf").read_bytes(), ".pdf")


parsed, err = asyncio.run(parse_fake_resume())
check("Questlight's real parser reads a fake resume", err is None and parsed.get("name") == "Rohit Verma", err)

jobs, err = asyncio.run(matching.fetch_open_jobs())
check("the real open-jobs list loads", err is None and len(jobs) > 0, err)


def rejected_for(applicant: dict):
    r = httpx.post(f"{questlight.api_base()}/applicants/create-applicant", headers=headers,
                   files=[("applicantData", (None, json.dumps(applicant)))], timeout=60)
    return r.status_code, questlight._field_errors(r.json())


if headers:
    # A profile built by our code from nasty data (over-long text, empty company, odd years...) plus an INVALID email.
    nasty = {
        "name": "ZZ Probe Invalid", "email": "not-an-email", "phoneNumber": "+91 90000 12345",
        "technical_skills": ["Python"], "total_experience": {"years": "3", "months": -2},
        "workExperience": [
            {"jobTitle": "Principal " * 15, "companyName": "Acme " * 30, "startDate": "2020-01-01", "endDate": "2021-01-01", "responsibilities": "one\ntwo"},
            {"companyName": "Walmart", "startDate": "2018-01-01", "duration": "2 years"}, {"jobTitle": "", "companyName": "", "startDate": "2016-01-01"}],
        "education": [{"degree": "", "institution": "", "graduation_year": "May 2019"}, {"institution": "Only An Institution"}],
        "certifications": [{"certificationName": "Certified " * 20, "dateObtained": "2022-03-01"}],
    }
    status, errors = rejected_for(questlight.build_applicant(nasty, fill=True))
    check("built by our code, the ONLY thing Questlight complains about is the email", status == 400 and errors == ["email must be an email"], errors)

    # The same shape of data WITHOUT our fixes, to prove this probe can see those complaints at all.
    raw = {"name": "ZZ Probe Invalid", "email": "not-an-email", "phoneNumber": "+91 90000 12345", "skills": ["Python"],
           "workExperience": [{"jobTitle": "Principal " * 15, "companyName": "", "startDate": "2020-01-01T00:00:00.000Z"}],
           "education": [{"degree": "B.Tech", "institution": "", "graduation_year": "May 2019"}]}
    status, errors = rejected_for(raw)
    check("without our fixes Questlight does complain about those fields (the probe works)",
          status == 400 and any("jobTitle" in e for e in errors) and any("companyName" in e for e in errors)
          and any("institution" in e for e in errors) and any("graduation_year" in e for e in errors), errors)

# ---- job descriptions: the real JD parser (stateless), and a job body Questlight MUST reject ----
from app.intake import jobs  # noqa: E402

jd_text = (ROOT / "tests" / "fixtures" / "jds" / "files" / "d03_jd_structured.txt").read_text(encoding="utf-8")  # invented
parsed_jd, err = asyncio.run(jobs.parse("jd.txt", b"", ".txt", jd_text))
parsed_jd = parsed_jd or {}
check("Questlight's real JD parser reads an invented JD", err is None and "QA" in str(parsed_jd.get("jobPositionTitle")) and parsed_jd.get("primarySkills"), err)
if headers:
    details = jobs.from_parsed(parsed_jd or {})
    built = jobs.review({**details, "client_id": "00000000-0000-4000-8000-000000000000", "business_head_id": "00000000-0000-4000-8000-000000000000",
                         "recruiter_id": "00000000-0000-4000-8000-000000000000", "country_id": "101", "state_id": "4026", "city_id": "57933"})["body"]
    built["jobPositionTitle"] = 12345  # the one deliberate fault: Questlight must refuse, so nothing is created
    r = httpx.post(f"{questlight.api_base()}/jobs/create", headers=headers, json=built, timeout=60)
    msgs = (r.json() if r.headers.get("content-type", "").startswith("application/json") else {}).get("errorMessage") or []
    check("a job body built by our code: Questlight's only complaint is the deliberately wrong title",
          r.status_code == 400 and msgs == ["jobPositionTitle must be a string"], (r.status_code, msgs))

print(f"(parser: {settings.PARSER}; nothing was created in Questlight)")
finish()
