"""The audit log: one row per decision, no candidate data, and a broken database never stops intake.
Throwaway database, a mock Questlight and a stubbed parser."""
import copy
import json
import os
import sqlite3

import harness

tmp = harness.isolate(QUESTLIGHT_TOKEN="good-token", RESUME_PARSER="perfox")
from harness import JUNK_FILES, check, finish, serve  # noqa: E402

from fastapi import FastAPI, Request  # noqa: E402
from fastapi.responses import JSONResponse  # noqa: E402

mock = FastAPI()


@mock.post("/api/applicants/create-applicant")
async def create(request: Request):
    form = await request.form()
    if json.loads(form["applicantData"])["email"] == "dup@example.com":
        return JSONResponse({"message": "exists"}, 409)
    return JSONResponse({"data": {"_id": "u1", "applicantId": "CAN-031026-00009"}}, 201)


questlight = serve(mock)   # has no jobs endpoint, so matching fails after a created profile: that is logged too
os.environ["QUESTLIGHT_BASE_URL"] = questlight.url + "/api"

from fastapi.testclient import TestClient  # noqa: E402

from app.intake import parser_perfox  # noqa: E402
from app.main import app  # noqa: E402
from fixtures.parsed import PRIYA, variant  # noqa: E402

client = TestClient(app)
current = {}


async def stub(name, data, ext):
    return copy.deepcopy(current["parsed"]), None


parser_perfox.parse_resume = stub


def upload(fname, parsed=None, data=None, name=None):
    current["parsed"] = parsed or PRIYA
    body = data if data is not None else (JUNK_FILES / fname).read_bytes()
    return client.post("/api/process", files={"file": (name or fname, body)}).json()


upload("r01_standard.pdf")                                        # accepted, created, matching fails (no jobs endpoint)
upload("r01_standard.pdf", variant(PRIYA, email=""))              # accepted, missing email
upload("r02_standard.docx", variant(PRIYA, email="dup@example.com"))  # accepted, duplicate
upload("j06_invoice.pdf")                                         # junk
upload("c01_cover_letter.pdf")                                    # needs_review
upload("s01_scanned.pdf")                                         # needs_review
upload("x", data=b"", name="empty.pdf")                           # error at intake
upload("x", data=b"hello", name="photo.png")                      # error at intake
upload("x", data=b"%PDF-1.4 broken", name="corrupt.pdf")          # error at junk_check

data = client.get("/api/log").json()
rows, summary = data["rows"], data["summary"]
check("screening decisions are counted", summary["junk_check"] == {"accepted": 3, "junk": 1, "needs_review": 2, "error": 1}, summary)
check("profile outcomes are counted", summary["load_questlight"] == {"created": 1, "not_created": 1, "duplicate": 1}, summary)
check("intake errors and parse results are counted", summary["intake"] == {"error": 2} and summary["parse"] == {"ok": 3}, summary)
check("matching is logged, here as failed (the mock has no jobs)", summary["match_roles"] == {"failed": 1}, summary)
check("the Questlight record id is on the row", any(r["record_id"] == "CAN-031026-00009" and r["step"] == "load_questlight" for r in rows))
runs = {}
for r in rows:
    runs.setdefault(r["run_id"], []).append(r["step"])
check("nine uploads are nine runs, with the steps each one reached",
      len(runs) == 9 and sorted(len(v) for v in runs.values()) == [1, 1, 1, 1, 1, 1, 3, 3, 4], sorted(len(v) for v in runs.values()))
check("every row is stamped with its channel", {r["channel"] for r in rows} == {"manual upload"})

# privacy: nothing about the candidate in the database file
raw = "\n".join(sqlite3.connect(os.environ["AUDIT_DB"]).iterdump())
# the whole phone number: a fragment like "98765" also turns up by chance inside the random hex ids
leaks = [w for w in ("priya.nair", "98765 43210", "Java", "Python", "Bengaluru", "Anna University", "Acme") if w in raw]
check(f"no candidate data in the database file (found: {leaks})", not leaks)
check("the log page is served", client.get("/log").status_code == 200)

# a broken audit database must not stop intake
good = os.environ["AUDIT_DB"]
os.environ["AUDIT_DB"] = str(tmp)  # a folder, not a file
r = upload("r01_standard.pdf")
check("audit database is a folder: the upload still completes", r["ok"] and r["profile"]["status"] == "created", r)
blocker = tmp / "blocker"
blocker.write_text("a file where a folder is needed")
os.environ["AUDIT_DB"] = str(blocker / "sub" / "audit.db")
r = upload("r01_standard.pdf")
check("audit folder can't be created (read-only disk, say): the upload still completes", r["ok"] and r["profile"]["status"] == "created", r)
os.environ["AUDIT_DB"] = str(tmp / "new-folder" / "audit.db")
upload("j06_invoice.pdf")
check("a missing folder is created on first use", (tmp / "new-folder" / "audit.db").exists())
os.environ["AUDIT_DB"] = good
finish()
