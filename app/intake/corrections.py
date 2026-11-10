"""Applies recruiter-supplied field and work-date corrections to parsed resume payloads."""
import calendar
import re
from datetime import date

from app.intake import questlight

_ENTRY_FIELDS = {"job_title": ("workExperience", "jobTitle"), "job_company": ("workExperience", "companyName"),
                 "degree": ("education", "degree"), "institution": ("education", "institution")}
_TOP_FIELDS = {"name": "name", "phone": "phoneNumber", "address": "address"}

MONTHS = {name: number for number, name in enumerate(("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"), 1)}
PRESENT = {"present", "current", "currently", "ongoing", "now", "today", "till date", "to date", "till now", "till today"}
DATE_HELP = "use a month and year, like 03/2020 or Mar 2020"
FIRST_YEAR = 1950


def parse_month(value) -> tuple[int, int] | str | None:
    """What a recruiter typed for a month: (year, month) from "03/2020", "3-2020", "2020-03" or "Mar 2020" (a month name in full
    works too), "present" for Present / current / till date, or None for anything else (a bare year is not enough)."""
    text = " ".join(str(value or "").lower().replace(",", " ").replace(".", " ").split())
    if text in PRESENT:
        return "present"
    if m := re.fullmatch(r"(\d{1,2})[\s/-]+(\d{4})", text):
        month, year = int(m.group(1)), int(m.group(2))
    elif m := re.fullmatch(r"(\d{4})[\s/-]+(\d{1,2})", text):
        year, month = int(m.group(1)), int(m.group(2))
    elif (m := re.fullmatch(r"([a-z]{3,9})[\s/-]*(\d{4})", text)) and m.group(1)[:3] in MONTHS:
        month, year = MONTHS[m.group(1)[:3]], int(m.group(2))
    else:
        return None
    return (year, month) if 1 <= month <= 12 and FIRST_YEAR <= year <= date.today().year + 1 else None


def date_problem(value, end: bool = False):
    """Why a typed month can't be used, or None when it can. A start has to be a real month that has already begun; an end may be
    Present, but not a month that hasn't happened yet."""
    got = parse_month(value)
    if got is None:
        return DATE_HELP + (", or Present" if end else "")
    if isinstance(got, str):  # "present"
        return None if end else "the start has to be a month and year, like 03/2020"
    today = date.today()
    return None if got <= (today.year, today.month) else "that month hasn't happened yet" + (": use Present if still working there" if end else "")


def month_start(year: int, month: int) -> str:
    return f"{year:04d}-{month:02d}-01"


def month_end(year: int, month: int) -> str:
    return f"{year:04d}-{month:02d}-{calendar.monthrange(year, month)[1]:02d}"


def span_problem(start, end):
    """Why this start and end can't both be true (the end before the start), or None. Present, or anything not yet valid, passes:
    each of those is checked on its own."""
    a, b = parse_month(start), parse_month(end)
    return "the end is before the start" if isinstance(a, tuple) and isinstance(b, tuple) and b < a else None


def apply_answer(parsed: dict, key: str, value: str):
    """Writes one answer into the parsed resume. Returns None when it worked, else why it did not."""
    if not value:
        return "the value is empty"
    if key in _TOP_FIELDS:
        parsed[_TOP_FIELDS[key]] = value
    elif key == "email":
        if not questlight.EMAIL_RE.match(value):
            return "that doesn't look like an email address"
        parsed["email"] = value
    elif key == "skills":
        have = {str(x).lower() for x in (parsed.get("technical_skills") or []) + (parsed.get("skills") or []) if isinstance(x, str)}
        new = [x.strip() for x in value.replace(";", ",").split(",") if x.strip() and x.strip().lower() not in have]
        parsed["technical_skills"] = list(parsed.get("technical_skills") or []) + new
    elif key.partition(":")[0] in ("job_start", "job_end"):
        kind, _, number = key.partition(":")
        entries = parsed.get("workExperience") or []
        if not number.isdigit() or int(number) >= len(entries) or not isinstance(entries[int(number)], dict):
            return "there is no such entry"
        job, end = entries[int(number)], kind == "job_end"
        problem = date_problem(value, end)
        if problem:
            return problem
        got = parse_month(value)
        if end and got == "present":
            job["isCurrent"] = True
            job.pop("endDate", None)
        elif isinstance(got, tuple):  # date_problem has already refused anything else
            year, month = got
            if end:
                if questlight._valid_date(job.get("startDate")) and month_end(year, month) < str(job["startDate"])[:10]:
                    return "the end is before the start"
                job["endDate"] = month_end(year, month)
                job.pop("isCurrent", None)
            else:
                if questlight._valid_date(job.get("endDate")) and not job.get("isCurrent") and month_start(year, month) > str(job["endDate"])[:10]:
                    return "the start is after the end"
                job["startDate"] = month_start(year, month)
    elif ":" in key and key.split(":", 1)[0] in _ENTRY_FIELDS:
        kind, _, number = key.partition(":")
        section, field = _ENTRY_FIELDS[kind]
        entries = parsed.get(section) or []
        if not number.isdigit() or int(number) >= len(entries) or not isinstance(entries[int(number)], dict):
            return "there is no such entry"
        entries[int(number)][field] = value
    elif key in ("workExperience", "education"):
        return "this can't be typed in here: the candidate needs a resume that shows it (with dates)"
    else:
        return "unknown id (use the ids from missing_items)"
    return None
