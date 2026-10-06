"""Builds the invented job descriptions in files/ for the JD tests (tests/test_jd_detect.py, tests/test_jd_flow.py), and
cases.json with the expected classification of each. Every company, person and number here is made up.
Only needed to change the cases: the files are checked in.

    pip install -r requirements-dev.txt
    python tests/fixtures/jds/generate.py
"""
import json
import struct
from pathlib import Path

from docx import Document
from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas

OUT = Path(__file__).parent / "files"
OUT.mkdir(exist_ok=True)
cases = []


def pdf(name, lines):
    c = canvas.Canvas(str(OUT / name), pagesize=A4)
    y = 800
    for ln in lines:
        if y < 50:
            c.showPage(); y = 800
        c.setFont("Helvetica-Bold" if ln.isupper() and len(ln) < 40 else "Helvetica", 10)
        c.drawString(50, y, ln[:110]); y -= 14
    c.save()


def docx(name, lines):
    d = Document()
    for ln in lines:
        d.add_paragraph(ln)
    d.save(str(OUT / name))


def txt(name, lines):
    (OUT / name).write_text("\n".join(lines), encoding="utf-8")


def doc(name, lines):
    """A stand-in for an old Word 97-2003 .doc: the OLE container header, some binary filler, and the text stored as
    UTF-16LE runs the way Word keeps it. Enough for the best-effort reader; not a file Word itself would open."""
    body = "\r".join(lines).encode("utf-16le")
    filler = bytes(range(256)) * 2
    data = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + struct.pack("<H", 0x3E) + filler + body + filler
    (OUT / name).write_bytes(data)


WRITERS = {"pdf": pdf, "docx": docx, "txt": txt, "doc": doc}


def add(name, expected, lines):
    WRITERS[name.rsplit(".", 1)[1]](name, lines)
    cases.append({"file": name, "expected": expected})


QA = [
    "Job Title: Senior QA Automation Engineer", "Company: Northwind Test Labs", "Location: Bengaluru, Karnataka, India (Hybrid)",
    "Experience: 4 to 8 years", "Employment type: Full-time   Openings: 2   Notice period: 1 month", "Salary: 12 - 18 LPA", "",
    "ABOUT THE ROLE",
    "We are looking for a QA Automation Engineer to build and maintain automated test suites for our web and API products.", "",
    "RESPONSIBILITIES",
    "- Design and maintain UI test automation with Selenium WebDriver and Java",
    "- Build API test suites with REST Assured and Postman", "- Integrate tests into Jenkins CI pipelines",
    "- Report defects in JIRA and work with developers on fixes", "",
    "REQUIREMENTS",
    "- 4+ years in software testing, 3+ in automation", "- Strong Java, Selenium, TestNG, REST Assured",
    "- Good to have: Cypress, Playwright, performance testing with JMeter", "- B.E./B.Tech in Computer Science or equivalent"]

add("d01_jd_structured.pdf", "job_description", QA)
add("d02_jd_structured.docx", "job_description", QA)
add("d03_jd_structured.txt", "job_description", QA)
add("d04_jd_old_word.doc", "job_description", QA)
add("d05_jd_prose.pdf", "job_description", [
    "Data Engineer - Pune (Remote friendly)",
    "Bluefin Analytics is growing its data platform team and we are hiring a Data Engineer. You will design batch and streaming",
    "pipelines on AWS, model data in Snowflake and keep our dbt projects healthy. The ideal candidate will have 3-6 years of",
    "experience with Python and SQL, hands-on Spark or Kafka, and a habit of writing tests. Compensation is 15-22 LPA with a",
    "notice period of up to 60 days. Immediate joiners preferred. How to apply: send your resume to talent@bluefin.example."])
add("d06_jd_short_pasted.txt", "job_description", [
    "Need a Java backend developer, 5+ years of experience, Spring Boot and microservices, Hyderabad, hybrid, budget 20 LPA,",
    "notice period 30 days max."])
add("d07_jd_with_contact.docx", "job_description", [
    "JOB DESCRIPTION", "Position: Sales Manager - FMCG", "Location: Mumbai, Maharashtra", "Experience required: 6+ years",
    "CTC: 14 - 20 LPA", "", "KEY RESPONSIBILITIES", "- Lead regional sales for FMCG distribution and grow revenue",
    "- Manage key retail accounts and distributor relationships", "- Build monthly forecasts and track targets", "",
    "PREFERRED QUALIFICATIONS", "- MBA in Sales or Marketing", "- Strong negotiation and CRM skills", "",
    "Contact: Priya Menon, HR, priya.menon@hr.example, +91 98450 12345"])

(Path(__file__).parent / "cases.json").write_text(json.dumps(cases, indent=1))
print(len(cases), "files written")
