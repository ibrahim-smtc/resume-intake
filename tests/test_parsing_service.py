"""The Perfox parser (app/parsing/service.py): the same reading whether the pipeline or a /parsing route asks. Perfox is faked, the
fake resumes and JDs are the ones in tests/fixtures. Nothing real is called."""
import asyncio

import harness

harness.isolate(PERFOX_BASE_URL="http://perfox.invalid", PERFOX_API_KEY="test-key")
from harness import FIXTURES, JUNK_FILES, RESUMES, check, finish  # noqa: E402

from fastapi.testclient import TestClient  # noqa: E402

from fixtures.parsed import ROHIT, variant  # noqa: E402

from app.main import app  # noqa: E402
from app.parsing import service  # noqa: E402

RESUME = (RESUMES / "Rohit_Verma_Python_Backend.pdf").read_bytes()
JD_PDF = (FIXTURES / "jds" / "files" / "d01_jd_structured.pdf").read_bytes()
JD_TEXT = (FIXTURES / "jds" / "files" / "d03_jd_structured.txt").read_text(encoding="utf-8")
USAGE = {"input_tokens": 100, "output_tokens": 50}
calls = []
answer = {"data": ROHIT, "usage": USAGE, "failure": None}


async def fake_perfox(name, data, ext, text, schema, prompt):
    calls.append({"name": name, "ext": ext, "bytes": len(data), "text_chars": len(text), "schema_props": len(schema.get("properties", {}))})
    import copy
    if answer["failure"]:
        return None, None, answer["failure"]
    return copy.deepcopy(answer["data"]), answer["usage"], None


service.call_perfox = fake_perfox

# ---- a resume ----
parsed, err = asyncio.run(service.parse_resume("Rohit_Verma_Python_Backend.pdf", RESUME, ".pdf"))
check("a resume is read and handed back as JSON, without the usage block", err is None and parsed["name"] == "Rohit Verma" and "usage" not in parsed, err)
check("Perfox was sent the PDF itself and the schema", calls[-1]["ext"] == ".pdf" and calls[-1]["bytes"] == len(RESUME) and calls[-1]["schema_props"] > 5, calls[-1])

answer["data"] = variant(ROHIT, email="", phoneNumber="")   # Perfox finds no email or phone, and the document shows none either
parsed, err = asyncio.run(service.parse_resume("no_contact.pdf", JD_PDF, ".pdf"))
check("a resume with no name, email or phone to read is reported with the message the pipeline asks the recruiter about",
      parsed is None and err.startswith(service.MISSING_MESSAGE), err)

blank = (JUNK_FILES / "j01_blank.pdf").read_bytes()
n = len(calls)
parsed, err = asyncio.run(service.parse_resume("blank.pdf", blank, ".pdf"))
answer["data"] = ROHIT
check("a file with no readable text is refused before Perfox is called", parsed is None and bool(err) and len(calls) == n, err)

answer["failure"] = (502, "PERFOX_REJECTED", "Perfox rejected the request: boom")
parsed, err = asyncio.run(service.parse_resume("Rohit_Verma_Python_Backend.pdf", RESUME, ".pdf"))
check("a Perfox failure comes back as its message", parsed is None and err == "Perfox rejected the request: boom", err)
answer["failure"] = None

# ---- a job description ----
answer["data"] = {"jobPositionTitle": "QA Engineer"}
body, err = asyncio.run(service.parse_jd("d01.pdf", JD_PDF, ".pdf", "ignored"))
check("a PDF JD goes up as the file", err is None and calls[-1]["ext"] == ".pdf" and calls[-1]["bytes"] == len(JD_PDF) and "jobPositionTitle" in body, (err, calls[-1]))
body, err = asyncio.run(service.parse_jd("d03.txt", b"", ".docx", JD_TEXT))
check("any other JD goes up as the text the caller read, named jd.txt", err is None and calls[-1]["name"] == "jd.txt" and calls[-1]["ext"] == ".txt"
      and calls[-1]["text_chars"] == len(JD_TEXT.strip()), (err, calls[-1]))
body, err = asyncio.run(service.parse_jd("jd.txt", b"", ".txt", "too short"))
check("a JD with almost no text is refused", body is None and err, err)

# ---- the routes read the same way ----
client = TestClient(app)
answer["data"] = ROHIT
r = client.post("/parsing/resume", files={"pdf_doc": ("Rohit.pdf", RESUME, "application/pdf")})
check("POST /parsing/resume gives the resume, with the usage block", r.status_code == 200 and r.json()["name"] == "Rohit Verma" and r.json()["usage"]["total_tokens"] == 150, r.text[:150])
answer["data"] = variant(ROHIT, email="", phoneNumber="")
r = client.post("/parsing/resume", files={"pdf_doc": ("no_contact.pdf", JD_PDF, "application/pdf")})
check("...and a 422 MISSING_REQUIRED_FIELDS when the details are missing", r.status_code == 422 and r.json()["error"]["error_type"] == "MISSING_REQUIRED_FIELDS")
answer["data"] = {"jobPositionTitle": "QA Engineer"}
r = client.post("/parsing/jd", data={"jd_text": JD_TEXT})
check("POST /parsing/jd reads pasted text", r.status_code == 200 and "jobPositionTitle" in r.json(), r.text[:150])
r = client.post("/parsing/jd")
check("...and refuses an empty request", r.status_code == 400 and r.json()["error"]["error_type"] == "NO_JOB_DESCRIPTION")
finish()
