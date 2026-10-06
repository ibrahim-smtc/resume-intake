"""The JD rules, offline: telling a JD from a resume (invented files in tests/fixtures/jds and tests/fixtures/junk), reading
TXT and old .doc files, turning the JD parser's answer into Questlight's create body (the rules found by probing
POST /jobs/create), and reading what a recruiter types ("12-18 LPA", "4-8 years")."""
import json
import re

import harness

harness.isolate()
from harness import FIXTURES, JUNK_FILES, check, finish  # noqa: E402

from app.intake import documents, jobs, junk  # noqa: E402
from fixtures.jds.parsed import BRIEF, JAVA, QA, variant  # noqa: E402

JD_FILES = FIXTURES / "jds" / "files"


def decide(path):
    text, has_images, err = documents.extract_text(path.read_bytes(), path.suffix.lower())
    return ("error: " + err) if err else junk.classify(text, has_images).decision


# ---------- what is it? ----------
jd_cases = json.loads((FIXTURES / "jds" / "cases.json").read_text())
wrong = [(c["file"], decide(JD_FILES / c["file"])) for c in jd_cases if decide(JD_FILES / c["file"]) != "job_description"]
check(f"all {len(jd_cases)} invented JDs are recognised (PDF, DOCX, TXT, old DOC, prose, a 2-line brief, one with an HR contact)", not wrong, wrong)
resumes = [c for c in json.loads((FIXTURES / "junk" / "cases.json").read_text()) if c["expected"] == "accepted"]
check("no resume is ever taken for a JD", all(decide(JUNK_FILES / c["file"]) != "job_description" for c in resumes),
      [c["file"] for c in resumes if decide(JUNK_FILES / c["file"]) == "job_description"])
check("a job ad is a JD now, not junk", decide(JUNK_FILES / "j08_job_ad.pdf") == "job_description")
check("invoices, spam and blank files are hard junk, whatever the recruiter calls them",
      all(junk.classify(documents.extract_text((JUNK_FILES / f).read_bytes(), ".pdf")[0]).signals["rule"] in junk.HARD_JUNK
          for f in ("j06_invoice.pdf", "j04_spam_promo.pdf", "j01_blank.pdf")))
check("a pasted two-line brief is a JD although it is short",
      junk.classify("Need a Java backend developer, 5+ years of experience, Spring Boot, Pune, hybrid, budget 20 LPA").decision == "job_description")

# ---------- reading TXT and old .doc ----------
text = "Senior QA Engineer – Bengaluru"
check("TXT: UTF-8, UTF-8 with BOM, UTF-16 and Latin-1 all read",
      documents.txt_text(text.encode("utf-8")) == text and documents.txt_text(b"\xef\xbb\xbf" + text.encode("utf-8")) == text
      and documents.txt_text(text.encode("utf-16")) == text and documents.txt_text("café".encode("latin-1")) == "café")
doc_text, _, err = documents.extract_text((JD_FILES / "d04_jd_old_word.doc").read_bytes(), ".doc")
check("an old Word .doc gives its text back", err is None and "Selenium WebDriver" in doc_text and "RESPONSIBILITIES" in doc_text, err)
check("a .doc that isn't a Word file, or has no readable text, is refused with a clear message",
      "isn't a Word document" in documents.extract_text(b"hello there", ".doc")[2]
      and "PDF or DOCX" in documents.extract_text(documents.OLE_MAGIC + bytes(range(256)) * 4, ".doc")[2])

# ---------- the JD parser's answer -> the details ----------
d = jobs.from_parsed(QA)
check("the parser's answer becomes details: title, client, place, ranges, skills",
      d["title"] == "Senior QA Automation Engineer" and d["client_name"] == "Northwind Test Labs" and d["city"] == "Bengaluru"
      and (d["min_exp"], d["max_exp"]) == (4, 8) and (d["salary_min"], d["salary_max"]) == (1200000, 1800000)
      and d["skills"] == ["Java", "Selenium WebDriver", "REST Assured", "TestNG"] and "Postman" in d["secondary_skills"], d)
check("work mode, job type and notice period map onto Questlight's values",
      d["work_mode"] == "Hybrid" and d["job_type"] == "Full-time" and d["notice_months"] == 1 and d["degree"] == "B.E."
      and jobs.from_parsed(JAVA)["job_type"] == "Full-time" and jobs.from_parsed(JAVA)["work_mode"] == "On-site")


def ready(details: dict) -> dict:
    """What prepare() would add from Questlight: the ids."""
    return {**details, "client_id": "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee", "business_head_id": "99999999-8888-4777-8666-555555555555",
            "recruiter_id": "11111111-2222-4333-8444-555555555555", "country_id": "101", "state_id": "4026", "city_id": "57933"}


r = jobs.review(ready(jobs.from_parsed(QA)))
body = r["body"]
REQUIRED = ["jobPositionTitle", "comments", "jobSummary", "clientId", "businessHead", "primaryRecruiter", "assignedRecruiters",
            "numberOfOpenings", "postingDate", "skillDomain", "primarySkills", "industry", "minExperience", "maxExperience",
            "priority", "salaryRangeMax", "salaryRangeMin", "country", "state", "city", "jobStatus"]
UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
check("a complete JD needs nothing more", r["missing"] == [] and r["items"] == [], r["missing"])
check("the body has every field Questlight's create call requires", all(k in body for k in REQUIRED), [k for k in REQUIRED if k not in body])
check("...with the types it checks: strings, UUIDs, a UUID list, positive numbers, a date, string lists",
      all(isinstance(body[k], str) and body[k] for k in ("jobPositionTitle", "comments", "jobSummary", "industry", "priority", "country", "state", "city", "jobStatus"))
      and all(UUID.match(body[k]) for k in ("clientId", "businessHead", "primaryRecruiter")) and all(UUID.match(x) for x in body["assignedRecruiters"])
      and isinstance(body["numberOfOpenings"], int) and body["numberOfOpenings"] >= 1
      and body["salaryRangeMin"] > 0 and body["salaryRangeMax"] >= body["salaryRangeMin"] and body["maxExperience"] >= body["minExperience"] >= 0
      and re.match(r"^\d{4}-\d{2}-\d{2}T", body["postingDate"]) and body["primarySkills"] and body["skillDomain"], body)
check("the job opens as Active with a valid priority, and the token's user is its recruiter",
      body["jobStatus"] == "Active" and body["priority"] in jobs.PRIORITIES and body["assignedRecruiters"] == [body["primaryRecruiter"]])
check("optional fields go along when the JD has them", body["remoteJob"] == "Hybrid" and body["jobType"] == "Full-time"
      and body["noticePeriod"] == 1 and body["degree"] == "B.E." and "closingDate" not in body)
sneaky = jobs.review(ready(jobs.from_parsed(variant(QA, jobSummary="Great role <script>alert(1)</script> & more"))))["body"]["jobSummary"]
check("text sent as rich text is escaped, so a JD can't carry markup", "<script>" not in sneaky and "&lt;script&gt;" in sneaky, sneaky)

b = jobs.review(jobs.from_parsed(BRIEF))
check("a thin brief lists what to ask: client, business head, location, salary", set(b["missing"]) >= {"client", "business_head", "location", "salary"}
      and all(i["can_supply"] for i in b["items"] if i["id"] != "recruiter"), b["missing"])
check("...and says what it filled in itself (maximum experience, openings, comments)",
      any("5-10 years" in a for a in b["adjusted"]) and any("openings" in a for a in b["adjusted"]) and any("comments" in a for a in b["adjusted"]), b["adjusted"])
odd = jobs.review(ready(jobs.from_parsed(variant(QA, minExperienceYears=9, maxExperienceYears=3, minimumSalary="2000000",
                                                  maximumSalary="1000000", closingDate="2020-01-01"))))
check("reversed ranges are swapped and a closing date in the past is dropped, each said out loud",
      (odd["body"]["minExperience"], odd["body"]["maxExperience"]) == (3, 9) and odd["body"]["salaryRangeMin"] < odd["body"]["salaryRangeMax"]
      and "closingDate" not in odd["body"] and len(odd["adjusted"]) >= 3, odd["adjusted"])

# ---------- what the recruiter types ----------
check("salary answers in the ways people write them", jobs.parse_salary("12-18 LPA") == (1200000, 1800000)
      and jobs.parse_salary("12 to 18 lakhs") == (1200000, 1800000) and jobs.parse_salary("1,200,000 - 1,800,000") == (1200000, 1800000)
      and jobs.parse_salary("20 LPA") == (2000000, 2000000) and jobs.parse_salary("80k - 1L") == (80000, 100000)
      and jobs.parse_salary("15-22") == (1500000, 2200000) and jobs.parse_salary("no idea") is None)
check("experience answers", jobs.parse_experience("4-8") == (4, 8) and jobs.parse_experience("8 to 4 years") == (4, 8)
      and jobs.parse_experience("5+") == (5, None) and jobs.parse_experience("at least 3 years") == (3, None) and jobs.parse_experience("senior") is None)
opts = [{"id": "1", "name": "ICICI Bank"}, {"id": "2", "name": "ICICI Lombard"}, {"id": "3", "name": "HDFC"}]
check("a client is picked only when exactly one fits (an exact name wins; two that contain it means ask)",
      jobs._pick(opts, "icici bank")["id"] == "1" and jobs._pick(opts, "HDFC Bank")["id"] == "3"
      and jobs._pick(opts, "ICICI") is None and jobs._pick(opts, "Zee") is None
      and jobs._pick(opts + [{"id": "4", "name": "ICICI"}], "icici")["id"] == "4")
xss = "%26%23106%26%2397  &#106&#97&#118&#97&#115&#99&#114&#105&#112&#116&#58&#99&#111&#110&#102&#105&#114&#109&#40&#49&#41"
check("names with injected script (as in the dev data) are never offered", not jobs._usable(jobs._clean_name(xss))
      and jobs._usable(jobs._clean_name("Inherit Technologies Pvt. Ltd.")))
finish()
