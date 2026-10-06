"""The JD intake end to end, through the agent's tools over real HTTP with the real MCP client: a JD (attached or pasted)
becomes a Questlight job, Questlight's matching is started, and the best candidates are screened for it. Also the
missing-details flow, duplicates, a JD dropped on process_resume and a resume dropped on process_job_description, junk,
failures, and what lands in the audit log. Questlight is a mock server here: nothing real is touched."""
import asyncio
import base64
import json
import math
import os
import sqlite3

import harness

harness.isolate()
from harness import FIXTURES, JUNK_FILES, check, finish, serve  # noqa: E402

from fastapi import FastAPI, Request  # noqa: E402
from fastapi.responses import JSONResponse  # noqa: E402

from fixtures.candidates import ALL  # noqa: E402
from fixtures.jds.parsed import BRIEF, JAVA, QA, variant  # noqa: E402
from fixtures.parsed import ROHIT  # noqa: E402

RECRUITER = "11111111-2222-4333-8444-555555555555"
LEADERS = [{"_id": "bh-meera", "firstName": "Meera", "lastName": "Rao"}, {"_id": "bh-arjun", "firstName": "Arjun", "lastName": "Iyer"}]
CLIENTS = [{"_id": "cl-northwind", "client_name": "Northwind Test Labs", "isActive": True, "isDeleted": False},
           {"_id": "cl-icici", "client_name": "ICICI Bank", "isActive": True, "isDeleted": False},
           {"_id": "cl-xss", "client_name": "%26%23106  &#106&#97&#118&#97&#115&#99&#114&#105&#112&#116&#58", "isActive": True}]
PLACES = {"bengaluru": ("101", "4026", "57933"), "pune": ("101", "4008", "133504"), "hyderabad": ("101", "4012", "132132")}
JD_FILES = FIXTURES / "jds" / "files"

state = {"parsed": QA, "parse_calls": [], "creates": [], "create_status": 201, "dups": set(), "dup_calls": 0,
         "matching_calls": [], "screen_calls": [], "status_calls": [], "on_job": [], "already": set()}
mock = FastAPI()


@mock.post("/parsing/jd")
async def parse_jd(request: Request):
    form = await request.form()
    state["parse_calls"].append({"text": str(form.get("jd_text") or ""), "file": getattr(form.get("file"), "filename", None)})
    return state["parsed"]


@mock.get("/api/location/resolve-ids")
async def resolve(city: str = "", state_: str = "", country: str = ""):
    ids = PLACES.get(city.lower(), ("101", None, None))
    return {"data": {"country_id": ids[0], "state_id": ids[1], "city_id": ids[2]}}


@mock.get("/api/clients")
async def clients():
    return {"data": CLIENTS}


@mock.get("/api/teams/leaders")
async def leaders():
    return {"data": LEADERS}


@mock.get("/api/jobs/all")
async def jobs_all():
    return {"data": []}


@mock.get("/api/jobs/check-duplicates")
async def duplicates(jobPositionTitle: str = "", clientId: str = "", city: str = ""):
    state["dup_calls"] += 1
    if jobPositionTitle in state["dups"]:
        return {"data": {"hasDuplicates": True, "duplicates": [{"jobId": "JOB-OLD-1", "jobPositionTitle": jobPositionTitle,
                                                                "clientName": "ICICI Bank", "city": "Bengaluru", "jobStatus": "Active"}]}}
    return {"data": {"hasDuplicates": False, "duplicates": []}}


@mock.post("/api/jobs/create")
async def create(body: dict):
    state["creates"].append(body)
    if state["create_status"] != 201:
        return JSONResponse({"statusCode": 400, "errorMessage": ["salaryRangeMax must be a positive number"]}, state["create_status"])
    n = len(state["creates"])
    return JSONResponse({"statusCode": 201, "data": {"_id": f"job-uuid-{n}", "jobId": f"JOB-T-{n:03d}"}}, 201)


@mock.post("/matching/semantic-score/job/{job_id}")
async def start_matching(job_id: str):
    state["matching_calls"].append(job_id)
    return {"status": "started"}


@mock.get("/api/applicants/findAll")
async def find_all(request: Request):
    page, limit = int(request.query_params["page"]), int(request.query_params["limit"])
    return {"data": {"data": ALL[(page - 1) * limit: page * limit], "total": len(ALL), "totalPages": math.ceil(len(ALL) / limit), "currentPage": page}}


@mock.get("/api/screening/job/{job_id}/existing-applicants")
async def on_job(job_id: str):
    return {"data": state["on_job"]}


@mock.post("/api/jobs/applicantMatching/create")
async def screen(body: dict):
    state["screen_calls"].append(body)
    if body["applicantId"] in state["already"]:
        return JSONResponse({"statusCode": 400, "message": "Applicant has already been mapped for this job"}, 400)
    return JSONResponse({"data": {}}, 201)


@mock.post("/api/applicants/change-status")
async def change_status(body: dict):
    state["status_calls"].append(body)
    return {"data": {}}


server = serve(mock)
os.environ["QUESTLIGHT_BASE_URL"] = server.url + "/api"
os.environ["PARSING_BASE_URL"] = server.url + "/parsing"     # read when settings is imported: set before the app loads
os.environ["MATCHING_BASE_URL"] = server.url + "/matching"
payload = base64.urlsafe_b64encode(json.dumps({"user_id": RECRUITER, "tenant_id": "t1"}).encode()).decode().rstrip("=")
os.environ["QUESTLIGHT_TOKEN"] = f"eyJhbGciOiJIUzUxMiJ9.{payload}.signature"
os.environ["QUESTLIGHT_BUSINESS_HEAD"] = "Meera Rao"

import agent_harness as ah  # noqa: E402

fakes = ah.start({"rohit": ROHIT},
                 files={"qa_jd": (JD_FILES / "d01_jd_structured.pdf").read_bytes(), "qa_txt": (JD_FILES / "d03_jd_structured.txt").read_bytes(),
                        "qa_doc": (JD_FILES / "d04_jd_old_word.doc").read_bytes(), "qa_docx": (JD_FILES / "d02_jd_structured.docx").read_bytes(),
                        "invoice": (JUNK_FILES / "j06_invoice.pdf").read_bytes(), "letter": (JUNK_FILES / "c01_cover_letter.pdf").read_bytes()})

from app.intake import candidates, jobs, matching  # noqa: E402
from app.observability import audit  # noqa: E402

JAVA_JD = ("Job title: Java Backend Developer at ICICI Bank, Bengaluru. We are looking for a developer with 4-8 years of experience "
           "in Java, Spring Boot and SQL. You will build microservices. Salary 15-22 LPA. Notice period 60 days. Must have: Docker.")
BRIEF_JD = "Need a Java developer, 5+ years of experience, Spring Boot, Bangalore, hybrid. Immediate joiners preferred."


def reset(**changes):
    state.update(parse_calls=[], creates=[], screen_calls=[], status_calls=[], matching_calls=[], dup_calls=0, **changes)
    candidates._cache["at"] = 0


async def main():
    o = {}
    async with ah.session(fakes) as s:
        call = lambda tool, **a: ah.call(s, tool, **a)  # noqa: E731

        # 1. a JD with everything in it, pasted: created, matched, screened
        reset(parsed=JAVA)
        o["java"] = await call("process_job_description", text=JAVA_JD)
        o["java_create"], o["java_parse"] = state["creates"][:], state["parse_calls"][:]
        o["java_screens"], o["java_status"], o["java_matching"] = state["screen_calls"][:], state["status_calls"][:], state["matching_calls"][:]
        reset()
        o["java_again"] = await call("process_job_description", text=JAVA_JD)
        o["java_again_creates"] = len(state["creates"])

        # 2. an attached PDF JD for a client with no fitting candidates: created, nobody screened
        reset(parsed=QA)
        o["qa"] = await call("process_job_description", file_url="https://files.test/qa_jd", file_name="QA JD.pdf")
        o["qa_parse"], o["qa_screens"] = state["parse_calls"][:], len(state["screen_calls"])
        o["qa_again"] = await call("process_job_description", file_url="https://files.test/qa_jd", file_name="QA JD.pdf")

        # 3. TXT, DOCX and old DOC files go up as text
        for kind in ("qa_txt", "qa_docx", "qa_doc"):
            reset(parsed=variant(QA, jobPositionTitle=f"QA from {kind}"))
            o[kind] = await call("process_job_description", file_url=f"https://files.test/{kind}",
                                 file_name={"qa_txt": "jd.txt", "qa_docx": "jd.docx", "qa_doc": "jd.doc"}[kind])
            o[kind + "_parse"] = state["parse_calls"][:]

        # 4. a thin brief: asks, takes the answers, then creates
        reset(parsed=BRIEF)
        o["brief"] = await call("process_job_description", text=BRIEF_JD)
        jd = o["brief"].get("jd_id")
        o["brief_creates"] = len(state["creates"])
        o["early_create"] = await call("create_job", jd_id=jd)
        o["bad_answers"] = await call("provide_job_details", jd_id=jd, answers=[{"id": "client", "value": "Acme"},
                                                                               {"id": "salary", "value": "lots"}, {"id": "colour", "value": "blue"}])
        o["answers"] = await call("provide_job_details", jd_id=jd, answers=[{"id": "client", "value": "icici bank"},
                                                                           {"id": "salary", "value": "18-24 LPA"},
                                                                           {"id": "skill_domains", "value": "Backend Development"}])
        o["brief_created"] = await call("create_job", jd_id=jd)
        o["brief_body"] = state["creates"][-1] if state["creates"] else None
        o["brief_create_again"] = await call("create_job", jd_id=jd)
        o["brief_creates_total"] = len(state["creates"])
        o["late_answers"] = await call("provide_job_details", jd_id=jd, answers=[{"id": "industry", "value": "IT"}])

        # 5. the same job is already open: ask, then create on request
        reset(parsed=variant(JAVA, jobPositionTitle="Duplicate Java Role"), dups={"Duplicate Java Role"})
        o["dup"] = await call("process_job_description", text=JAVA_JD + " (again)")
        o["dup_creates"] = len(state["creates"])
        o["dup_forced"] = await call("create_job", jd_id=o["dup"]["jd_id"], allow_duplicate=True)
        o["dup_forced_creates"] = len(state["creates"])
        state["dups"] = set()

        # 6. a JD dropped on process_resume, and a resume on process_job_description
        reset(parsed=variant(QA, jobPositionTitle="QA via process_resume"))
        o["jd_as_resume"] = await call("process_resume", file_url="https://files.test/qa_docx", file_name="role.docx")
        o["jd_as_resume_creates"] = len(state["creates"])
        reset()
        o["resume_as_jd"] = await call("process_job_description", file_url="https://files.test/rohit", file_name="Rohit_Verma.pdf")
        o["resume_as_jd_creates"], o["resume_as_jd_parsed"] = len(state["creates"]), list(fakes.parse_calls)
        o["resume_text_as_jd"] = await call("process_job_description",
                                            text=(JUNK_FILES.parent / "cases.json").read_text()[:0] + "Priya Nair\npriya@example.com +91 98765 43210\n"
                                            "EXPERIENCE\nSenior Engineer, Acme  Jan 2021 - Present\nEngineer, Nimbus  2017 - 2020\n"
                                            "EDUCATION\nB.Tech, Anna University, 2017\nSKILLS\nJava, Python")

        # 7. not a JD at all
        reset()
        o["invoice"] = await call("process_job_description", file_url="https://files.test/invoice", file_name="bill.pdf")
        o["letter"] = await call("process_job_description", file_url="https://files.test/letter", file_name="letter.pdf")
        o["nothing"] = await call("process_job_description")
        o["not_junk_creates"] = len(state["creates"])

        # 8. Questlight refuses the create; people already on the job; screening of someone further along
        reset(parsed=variant(JAVA, jobPositionTitle="Refused Role"), create_status=400)
        o["refused"] = await call("process_job_description", text=JAVA_JD + " refused")
        reset(parsed=variant(JAVA, jobPositionTitle="Busy Role"), create_status=201, on_job=["c-asha"])
        o["busy"] = await call("process_job_description", text=JAVA_JD + " busy")
        o["busy_screens"] = state["screen_calls"][:]
        reset(parsed=variant(JAVA, jobPositionTitle="Mapped Role"), on_job=[], already={"c-asha"})
        o["mapped"] = await call("process_job_description", text=JAVA_JD + " mapped")
        o["mapped_status"] = state["status_calls"][:]
        state.update(on_job=[], already=set())

        o["summary"] = await call("get_intake_summary", days=1)
        o["tools"] = {t.name for t in (await s.list_tools()).tools}
    return o


o = asyncio.run(main())

j = o["java"]
check("a full JD becomes a job: created, with its JOB- id", j["ok"] and j["kind"] == "job_description" and j["job_created"] and j["job_id"] == "JOB-T-001", j)
body = o["java_create"][0]
check("the job is built from the JD: title, client found by name, place ids, ranges in rupees and years",
      body["jobPositionTitle"] == "Java Backend Developer" and body["clientId"] == "cl-icici" and (body["country"], body["state"], body["city"]) == PLACES["bengaluru"]
      and (body["salaryRangeMin"], body["salaryRangeMax"]) == (1500000, 2200000) and (body["minExperience"], body["maxExperience"]) == (4, 8), body)
check("...the token's user is its recruiter, the business head comes from the setting, and it opens Active",
      body["primaryRecruiter"] == RECRUITER and body["assignedRecruiters"] == [RECRUITER] and body["businessHead"] == "bh-meera"
      and body["jobStatus"] == "Active" and body["priority"] == "High")
check("pasted text goes to the parser as jd_text, not as a file", o["java_parse"][0]["file"] is None and "Spring Boot" in o["java_parse"][0]["text"])
check("Questlight's own matching is started for the new job, as its Create Job page does", o["java_matching"] == ["job-uuid-1"])
top = j["top_candidates"]
check("the best candidates already in Questlight are ranked for it, the Java developer first", top and top[0]["candidate_id"] == "CAN-T-001" and top[0]["score"] >= 70, top)
check("...and the strong ones are put on the job at the Screening stage, by the long ids", j["screening"]["status"] == "screened"
      and o["java_screens"] and all(c["jobId"] == "job-uuid-1" and c["stages"] == "SCREENING" for c in o["java_screens"])
      and o["java_screens"][0]["applicantId"] == "c-asha" and len(o["java_screens"]) == len([t for t in top if t["score"] >= 40]), o["java_screens"])
check("hired and onboarding people are never screened", not ({"c-placed", "c-onb", "c-ready"} & {c["applicantId"] for c in o["java_screens"]}))
status_ids = {c["applicant_id"] for c in o["java_status"]}
check("only NEW_CANDIDATE profiles are moved to SCREENING; someone further along keeps their status",
      "c-asha" in status_ids and not ({"c-ravi", "c-lena", "c-junior"} & status_ids), o["java_status"])
check("the same pasted JD again does not create a second job", o["java_again"]["job_status"] == "created" and o["java_again_creates"] == 0
      and o["java_again"]["jd_id"] == j["jd_id"])

q = o["qa"]
check("an attached PDF JD: the file itself goes to the parser", q["job_created"] and o["qa_parse"][0]["file"] == "QA JD.pdf", o["qa_parse"])
strong = [t["candidate_id"] for t in q["top_candidates"] if t["score"] >= 40]
check("only candidates who are a strong match themselves are screened: a 15-point sales manager is not put on a QA job",
      q["screening"]["status"] == "screened" and o["qa_screens"] == len(strong) >= 1
      and all(t["screening"] is None for t in q["top_candidates"] if t["score"] < 40), q["top_candidates"])
check("the same file again (an ordinary https link) is recognised by its content and not run again",
      "already taken in" in o["qa_again"].get("reason", "") and o["qa_again"]["job_id"] == q["job_id"], o["qa_again"])
check("TXT, DOCX and old DOC JDs are read here and sent as text", all(o[k]["job_created"] and o[k + "_parse"][0]["file"] is None
      and "Selenium" in o[k + "_parse"][0]["text"] for k in ("qa_txt", "qa_docx", "qa_doc")), [o[k].get("job_status") for k in ("qa_txt", "qa_docx", "qa_doc")])

b = o["brief"]
check("a thin brief is not created: it asks for the client and the salary", b["job_status"] == "not_created" and o["brief_creates"] == 0
      and {i["id"] for i in b["missing_items"]} == {"client", "salary", "skill_domains"} and "provide_job_details" in b["next"], b)
check("...the old city name was looked up as its new one (Bangalore -> Bengaluru), so the place isn't asked for",
      "location" not in {i["id"] for i in b["missing_items"]})
check("create_job refuses while something is missing", o["early_create"]["job_status"] == "not_created")
bad = o["bad_answers"]
check("unknown clients, unreadable salaries and made-up ids are rejected, with the client list to choose from",
      bad["applied"] == [] and len(bad["rejected"]) == 3 and "ICICI Bank" in bad["rejected"][0] and "javascript" not in json.dumps(bad), bad)
check("good answers are taken and nothing is missing any more", o["answers"]["applied"] == ["client", "salary", "skill_domains"]
      and o["answers"]["info_complete"], o["answers"])
check("create_job then creates it with the answers", o["brief_created"]["job_created"] and o["brief_body"]["clientId"] == "cl-icici"
      and (o["brief_body"]["salaryRangeMin"], o["brief_body"]["salaryRangeMax"]) == (1800000, 2400000) and o["brief_body"]["minExperience"] == 5)
check("...and says what it filled in (no maximum experience, openings)", any("5-10" in a for a in o["brief_created"]["adjusted"]))
check("create_job again returns the saved job and never creates a second", o["brief_create_again"]["job_id"] == o["brief_created"]["job_id"]
      and o["brief_creates_total"] == 1)
check("details can't be changed once the job exists", o["late_answers"]["ok"] is False)

check("an open job with the same title, client and city stops the create and is shown", o["dup"]["job_status"] == "duplicate_found"
      and o["dup_creates"] == 0 and o["dup"]["duplicates"][0]["job_id"] == "JOB-OLD-1" and "allow_duplicate" in o["dup"]["next"])
check("...and only allow_duplicate creates it anyway", o["dup_forced"]["job_created"] and o["dup_forced_creates"] == 1)

check("a JD dropped on process_resume is taken in as a JD", o["jd_as_resume"]["kind"] == "job_description" and o["jd_as_resume"]["job_created"]
      and o["jd_as_resume_creates"] == 1, o["jd_as_resume"])
check("a resume dropped on process_job_description is taken in as a resume", o["resume_as_jd"]["kind"] == "resume"
      and o["resume_as_jd"]["decision"] == "accepted" and o["resume_as_jd_creates"] == 0 and o["resume_as_jd"].get("profile_created"), o["resume_as_jd"])
check("a resume pasted as text is refused: resumes have to be files", o["resume_text_as_jd"]["ok"] is False and o["resume_text_as_jd"]["kind"] == "resume")
check("an invoice is junk and a cover letter is left for a person; nothing is created", o["invoice"]["kind"] == "junk"
      and o["letter"]["kind"] == "unclear" and o["not_junk_creates"] == 0 and o["nothing"]["ok"] is False)

check("Questlight refusing the create is reported with its reason", o["refused"]["job_status"] == "failed" and "salaryRangeMax" in o["refused"]["job_message"])
busy = {c["applicantId"] for c in o["busy_screens"]}
check("someone already on the job is not suggested again", "c-asha" not in busy and all(t["candidate_id"] != "CAN-T-001" for t in o["busy"]["top_candidates"]))
check("'already mapped' from Questlight counts as done, not as a failure, and the status is left alone",
      o["mapped"]["top_candidates"][0]["screening"] == "already" and o["mapped"]["screening"]["status"] == "screened"
      and not any(c["applicant_id"] == "c-asha" for c in o["mapped_status"]), o["mapped"]["top_candidates"])

summary = o["summary"]
check("the summary counts JDs and jobs", sum(summary["jobs"].values()) >= 8 and summary["jobs"].get("created", 0) >= 7
      and summary["job_descriptions"].get("job_description", 0) >= 6, summary)
check("the agent sees the three JD tools", {"process_job_description", "provide_job_details", "create_job"} <= o["tools"])
raw = "\n".join(sqlite3.connect(os.environ["AUDIT_DB"]).iterdump())
check("no candidate names, JD text or salaries in the audit database", not any(w in raw for w in ("Asha Rao", "Ravi Sales", "Spring Boot", "1500000", "Selenium")),
      [w for w in ("Asha Rao", "Ravi Sales", "Spring Boot", "1500000", "Selenium") if w in raw])
steps = {r["step"] for r in audit.recent(1000)}
check("each step is in the audit log", {"jd_check", "jd_parse", "create_job", "job_matching", "find_candidates_for_new_job", "screen_candidates", "job_details"} <= steps, steps)
finish()
