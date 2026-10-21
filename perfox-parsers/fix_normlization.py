"""perfox-parsers/fixup.py
Post-processing normalization for parsed resumes:
- Deduplicates certification names from technical skills
- Clamps end dates to today for current/ongoing roles
- Accurately computes per-job durations and non-overlapping total experience
"""

import re
from datetime import date


def _to_date(value, today: date) -> date | None:
    """Safely parses an ISO date string (YYYY-MM-DD) and clamps future dates to today."""
    if not value:
        return None
    try:
        d = date.fromisoformat(str(value)[:10])
    except (ValueError, TypeError):
        return None
    return min(d, today)  # End date cannot be in the future

def _years_months(days: int) -> dict[str, int]:
    """Converts a total number of days into {years, months} using average month length."""
    months = max(0, round(days / 30.4375))
    return {"years": months // 12, "months": months % 12}


def fix_dates_and_experience(parsed: dict, today: date) -> dict:
    """Normalizes the extracted resume dict in-place and returns it."""
    if not isinstance(parsed, dict):
        return parsed

    # 1. Clean taxonomy: deduplicate certification names from technical skills
    cert_names = {
        cert.get("certificationName", "").strip().lower()
        for cert in (parsed.get("certifications") or [])
        if isinstance(cert, dict) and cert.get("certificationName")
    }
    if cert_names and parsed.get("technical_skills"):
        parsed["technical_skills"] = [
            skill for skill in parsed["technical_skills"]
            if isinstance(skill, str) and skill.strip().lower() not in cert_names
        ]

    # 2. Fix ongoing jobs and calculate durations
    spans: list[tuple[date, date]] = []
    for job in parsed.get("workExperience") or []:
        if not isinstance(job, dict):
            continue
        start = _to_date(job.get("startDate"), today)
        if job.get("isCurrent"):
            end = today
            job["endDate"] = today.isoformat()
        else:
            end = _to_date(job.get("endDate"), today) if job.get("endDate") else today
        if start and end and end >= start:
            job["duration"] = _years_months((end - start).days)
            spans.append((start, end))

# 3. Non-overlapping experience merge
# Overlapping roles (e.g. freelance + full-time) are only counted once towards total experience
    spans.sort()
    merged: list[list[date]] = []
    for start, end in spans:
        if merged and start <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    total_days = sum((end - start).days for start, end in merged)
    parsed["total_experience"] = _years_months(total_days)

    return parsed

# 

EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+")
# A run of digits with the usual separators, on one line (so digits on the next line are never joined on). Whether it is
# really a phone number is decided in find_phone().
PHONE_RE = re.compile(r"\+?\(?\d[\d ().\t-]{8,}\d")


def _is_year(token: str) -> bool:
    return len(token) == 4 and 1900 <= int(token) <= 2100


def _balanced(span: str) -> str:
    """Drops a bracket that has no partner: '+91) 98765 43210' -> '+91 98765 43210'."""
    while span.count(")") > span.count("("):
        span = span.replace(")", "", 1)
    while span.count("(") > span.count(")"):
        span = span.replace("(", "", 1)
    return span


def phone_spans(text: str):
    """(start, end) of every phone-like number in the text, in order: 10 to 13 digits (a local number up to one with a country
    code). Digits on the next line are never joined on, and a run holding two or more years is a date range, not a phone number
    ('2019 - 2023 - 2024'). A number is cut out of a longer run of digits: '2019 2023 98765 43210' gives only '98765 43210'."""
    for run in PHONE_RE.finditer(text):
        candidate = run.group(0)
        tokens = list(re.finditer(r"\d+", candidate))
        i = 0
        while i < len(tokens):
            digits, j = 0, i
            while j < len(tokens):
                digits += len(tokens[j].group(0))
                if digits >= 10:
                    break
                j += 1
            found = j < len(tokens) and 10 <= digits <= 13 and sum(_is_year(t.group(0)) for t in tokens[i:j + 1]) < 2
            if not found:
                i += 1
                continue
            start, end = tokens[i].start(), tokens[j].end()
            if start > 0 and candidate[start - 1] in "+(":
                start -= 1
            if candidate[end:end + 1] == ")":
                end += 1
            yield run.start() + start, run.start() + end
            i = j + 1


def phone_numbers(text: str):
    """Every phone-like number in the text, in order, written as it appears there (see phone_spans)."""
    for start, end in phone_spans(text):
        yield _balanced(text[start:end])


def find_phone(text: str) -> str | None:
    """The first phone-like number in the text (see phone_numbers), or None."""
    return next(phone_numbers(text), None)


def tidy_education(parsed: dict) -> dict:
    """Drops an education entry that has no institution (a school level 'HSC' with nothing after it, say), but only while at least
    one other entry that has an institution remains. If no entry has one, they are all kept: the main app then asks the recruiter."""
    if not isinstance(parsed, dict) or not isinstance(parsed.get("education"), list):
        return parsed
    has_institution = [isinstance(e, dict) and str(e.get("institution") or "").strip() != "" for e in parsed["education"]]
    if any(has_institution):
        parsed["education"] = [e for e, keep in zip(parsed["education"], has_institution) if keep]
    return parsed


def check_required(parsed: dict, text: str) -> tuple[dict, list[str]]:
    """Validates presence of name, email, and phone number.
    Falls back to regex extraction from raw text if model omitted email/phone.
    Returns (parsed, missing_fields_list).
    """

    if not isinstance(parsed, dict):
        return parsed, ["name", "email", "phoneNumber"]
    
    # 1. Email fallback via regex
    current_email = str(parsed.get("email") or "").strip()
    if not current_email or "@" not in current_email:
        email_match = EMAIL_RE.search(text)
        if email_match:
            parsed["email"] = email_match.group(0).strip()

    # 2. Phone fallback via regex
    current_phone = str(parsed.get("phoneNumber") or "").strip()
    if not current_phone:
        phone = find_phone(text)
        if phone:
            parsed["phoneNumber"] = phone

    # 3. Check required fields
    missing: list[str] = []
    if not str(parsed.get("name") or "").strip():
        missing.append("name")
    if not str(parsed.get("email") or "").strip():
        missing.append("email")
    if not str(parsed.get("phoneNumber") or "").strip():
        missing.append("phoneNumber")
    return parsed, missing
