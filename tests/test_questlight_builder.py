"""How a parsed resume becomes a Questlight profile (app/intake/questlight.py) and how a recruiter's answers are applied
(app/intake/corrections.py). Pure logic: nothing is sent anywhere. The rules mirror Questlight's own validator, which
test_live.py checks against the real service."""
import copy
import json

import harness

harness.isolate()
from harness import check, finish  # noqa: E402

from app.intake import corrections, questlight  # noqa: E402
from app.observability import tracing  # noqa: E402

LONG = "Senior Principal Staff Engineer of Distributed Systems, Data Platforms and Machine Learning Infrastructure Group"
NASTY = {
    "name": "ZZ Probe", "email": "probe@example.com", "phoneNumber": "+91 90000 12345",
    "technical_skills": ["Python"], "total_experience": {"years": "3", "months": -2},
    "workExperience": [
        {"jobTitle": LONG, "companyName": "Acme " + "Industries " * 15, "startDate": "2020-01-01", "endDate": "2021-01-01", "responsibilities": "x"},
        {"jobTitle": "Freelance Developer", "companyName": "", "startDate": "2021-02-01"},
        {"jobTitle": "Intern", "companyName": None, "startDate": "2019-06-01"},
        {"companyName": "Walmart Global Tech India", "startDate": "2018-01-01", "duration": "2 years"},
        {"jobTitle": "", "companyName": "Tata Consultancy Services", "startDate": "2016-01-01", "duration": {"years": -1, "months": "4"}}],
    "education": [
        {"degree": "B.Tech " + "in Computer Science and Engineering " * 4, "institution": "", "graduation_year": "May 2019"},
        {"degree": "12th", "institution": "Some School", "graduation_year": "pursuing"},
        {"degree": "Diploma", "institution": "Poly", "graduation_year": 2016},
        {"institution": "Only An Institution University"}],
    "certifications": [{"certificationName": "Certified " + "Cloud Architect " * 10, "institution": "Vendor " * 30, "dateObtained": "2022-03-01"}],
}

# ---- fill=True: every gap becomes "Not specified" and the payload fits Questlight's rules ----
before = json.dumps(NASTY)
changes = []
with tracing.Trace("t1").span("probe", kind="chain") as span:
    applicant = questlight.build_applicant(NASTY, changes, fill=True)
    recorded = span.details.get("adjusted_for_questlight")
jobs, edu, cert = applicant["workExperience"], applicant["education"], applicant["certifications"][0]
check("the parsed input is not modified", json.dumps(NASTY) == before)
check("text fields fit the 100 characters Questlight allows",
      all(len(j["jobTitle"]) <= 100 and len(j["companyName"]) <= 100 for j in jobs) and all(len(e["degree"]) <= 100 for e in edu)
      and len(cert["certificationName"]) <= 100 and len(cert["institution"]) <= 100)
check("empty company names become 'Not specified'", [j["companyName"] for j in jobs[1:3]] == ["Not specified", "Not specified"])
check("jobs without a title become 'Not specified'", [j["jobTitle"] for j in jobs[3:]] == ["Not specified", "Not specified"])
check("empty institution and degree become 'Not specified'", edu[0]["institution"] == "Not specified" and edu[3]["degree"] == "Not specified")
check("graduation_year is a number, or absent", [e.get("graduation_year") for e in edu] == [2019, None, 2016, None], edu)
check("responsibilities text becomes a list", jobs[0]["responsibilities"] == ["x"])
check("a text duration is left out; a messy one becomes whole numbers >= 0",
      "duration" not in jobs[3] and jobs[4]["duration"] == {"years": 0, "months": 4}, jobs[3:])
check("total_experience becomes whole numbers >= 0", applicant["total_experience"] == {"years": 3, "months": 0}, applicant.get("total_experience"))
check("the caller gets the notes, and so does the trace, by field name only",
      changes == recorded and "a job title was empty" in changes and not any(w in " ".join(changes) for w in ("Acme", "Walmart", "Vendor")), changes)

# ---- fill=False (the default): gaps are reported with ids a recruiter can fill, not hidden ----
rev = questlight.review(NASTY)
ids = sorted(i["id"] for i in rev["items"])
check("review() lists the gaps, each with an id", ids == ["degree:3", "institution:0", "job_company:1", "job_company:2", "job_title:3", "job_title:4"], ids)
check("review() names the job in plain words", "a job title for the job at Walmart Global Tech India" in rev["missing"], rev["missing"])
check("every gap can be supplied by the recruiter", all(i["can_supply"] for i in rev["items"]))
filled = questlight.review(NASTY, fill=True)
check("review(fill=True) has nothing missing, and says what it filled", filled["missing"] == [] and "a job title was empty" in filled["adjusted"])

# ---- what blocks a profile outright ----
gone = {"name": " ", "email": "not an email", "technical_skills": [], "workExperience": [], "education": []}
rev = questlight.review(gone)
check("name, email, skills, work history and education are all required",
      sorted(i["id"] for i in rev["items"]) == ["education", "email", "name", "skills", "workExperience"], rev["items"])
check("only name, email and skills can be typed in", {i["id"]: i["can_supply"] for i in rev["items"]} ==
      {"name": True, "email": True, "skills": True, "workExperience": False, "education": False})
check("a job without a valid start date is not sent (and so cannot satisfy 'work history')",
      questlight.review({"name": "A", "email": "a@b.co", "technical_skills": ["x"], "education": [{"degree": "d", "institution": "i"}],
                         "workExperience": [{"jobTitle": "t", "companyName": "c", "startDate": "garbage"}]})["items"][0]["id"] == "workExperience")
check("phone is only recommended", questlight.missing_recommended({"phoneNumber": ""}) == ["phoneNumber"]
      and questlight.missing_recommended({"phoneNumber": "+91 98765 43210"}) == [])

# ---- when the parser could not read the contact details at all ----
unreadable = lambda msg: [i["id"] for i in questlight.unreadable_items(msg)]  # noqa: E731
check("Questlight's 422 message says which details to ask for", unreadable("We couldn't find the following details: email") == ["email"]
      and unreadable("We couldn't find the following details: name, phone") == ["name", "phone"] and unreadable("something else") == ["name", "email"])

# ---- the recruiter's answers ----
parsed = copy.deepcopy(NASTY)
apply = lambda key, value: corrections.apply_answer(parsed, key, value)  # noqa: E731
check("a job title is written to the entry it names", apply("job_title:3", "Data Engineer") is None and parsed["workExperience"][3]["jobTitle"] == "Data Engineer")
check("company, degree and institution work the same way", apply("job_company:1", "Acme") is None and apply("degree:3", "B.E") is None
      and apply("institution:0", "NIT") is None and parsed["education"][3]["degree"] == "B.E")
check("name, phone and address", apply("name", "New Name") is None and apply("phone", "+91 1") is None and apply("address", "Pune") is None
      and (parsed["name"], parsed["phoneNumber"], parsed["address"]) == ("New Name", "+91 1", "Pune"))
check("skills are added, without duplicates", apply("skills", "Python, SQL; Airflow") is None and parsed["technical_skills"] == ["Python", "SQL", "Airflow"], parsed["technical_skills"])
check("a bad email, an empty value, a missing entry and an unknown id are all refused",
      "email" in apply("email", "nope") and "empty" in apply("name", "") and "no such entry" in apply("job_title:99", "x")
      and "no such entry" in apply("job_title:x", "x") and "unknown id" in apply("salary", "1"))
check("work history and education can't be typed in", "can't be typed" in apply("workExperience", "x") and "can't be typed" in apply("education", "x"))
finish()
