"""Hand-made Questlight jobs, in the shape of GET /api/jobs/all, so the tests never need the real job list."""
import time
from datetime import date


def job(_id, title, primary, secondary=(), lo=0, hi=0, degree="", city="Bangalore", state="Karnataka", status="Active",
        closing=None, deleted=False, summary=""):
    return {"_id": _id, "jobId": f"JOB-{_id}", "jobPositionTitle": title, "primarySkills": list(primary),
            "secondarySkills": list(secondary), "minExperience": lo, "maxExperience": hi, "degree": degree, "city": city,
            "state": state, "workLocation": "", "jobStatus": status, "closingDate": closing, "isDeleted": deleted,
            "jobSummary": f"<p>{summary or title}</p>"}


# J1, J2, J3, J7, J8 are open. J4 is closed, J5 is past its closing date, J6 is deleted: none of those may ever match.
JOBS = [
    job("J1", "Java Backend Developer", ["java", "spring boot", "sql"], ["docker"], 4, 8, "B.Tech/BE",
        summary="Build microservices with Java and Spring Boot, REST APIs, PostgreSQL, deployed on AWS with Docker."),
    job("J2", "Sales Manager FMCG", ["sales", "negotiation", "crm"], [], 6, 12, "MBA", "Mumbai", "Maharashtra",
        summary="Lead regional sales for FMCG distribution, grow revenue, manage retail accounts and forecasting."),
    job("J3", "Data Scientist", ["python", "machine learning", "pandas"], [], 2, 6, "M.Sc", summary="Models and analytics."),
    job("J4", "Java Architect", ["java", "spring boot", "aws"], [], 8, 12, "B.Tech", status="Closed by us"),
    job("J5", "Java Developer (old)", ["java", "spring boot"], [], 4, 8, closing="2024-01-01"),
    job("J6", "Java Lead (deleted)", ["java", "spring boot"], [], 4, 8, deleted=True),
    job("J7", "Frontend Developer", ["react", "javascript", "css"], [], 2, 5, "B.Tech", summary="React web apps."),
    job("J8", "Laboratory Analyst", ["hplc", "gc", "documentation"], [], 2, 6, "M.Sc", "Kochi", "Kerala",
        summary="Pharma lab testing with HPLC and GC, reports and documentation."),
]
OPEN_JOB_COUNT = 5


def prime(matching, jobs=None) -> None:
    """Fills the matcher's job cache with these jobs (only the open ones, as a real download would), so nothing is fetched."""
    today = date.today()
    matching._cache.update(at=time.time(), jobs=[j for j in (jobs or JOBS) if matching._is_open(j, today)])
