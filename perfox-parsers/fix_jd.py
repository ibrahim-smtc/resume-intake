"""perfox-parsers/fix_jd.py
Deterministic post-processing and normalization for parsed Job Descriptions.
Produces exactly 30 standardized fields matching the Questlight specification.
"""

import re
from datetime import date, datetime


# Salary multiplier units for Indian compensation formats
_SALARY_UNITS = {
    "lpa": 100000, "lakh": 100000, "lakhs": 100000, "lac": 100000, "lacs": 100000, "l": 100000,
    "cr": 10000000, "crore": 10000000, "crores": 10000000,
    "k": 1000, "thousand": 1000,
}
_JOB_TYPES = [
    ("full-time", "Full-time"), ("full time", "Full-time"), ("fulltime", "Full-time"), ("permanent", "Full-time"),
    ("part-time", "Part-time"), ("part time", "Part-time"),
    ("contract", "Contract"), ("contractual", "Contract"), ("freelance", "Contract"), ("temporary", "Contract"), ("c2h", "Contract"),
]
_WORK_MODES = [
    ("remote", "Remote"), ("wfh", "Remote"), ("work from home", "Remote"),
    ("hybrid", "Hybrid"),
    ("on-site", "On-site"), ("onsite", "On-site"), ("office", "On-site"), ("in-office", "On-site"),
]


def parse_salary(text: str | None) -> tuple[str, str]:
    """Parses salary text into (minimumSalary, maximumSalary) as annual rupee digit strings or ('', '').
    Handles LPA, lakhs, cr, k/thousand, monthly salaries (* 12), bare numbers (< 200 as LPA), and reversed ranges.
    """

    if not text:
        return ("", "")
    t = str(text).lower().replace(",", "")
    is_monthly = bool(re.search(r"\b(per month|a month|monthly|/month|/mo|pm)\b", t))
    nums = [
        (float(n), (u or "").strip("."))
        for n, u in re.findall(r"(\d+(?:\.\d+)?)\s*(lpa|lakhs?|lacs?|crores?|cr|k|thousand|l\b)?", t)
    ]

    if not nums:
        return ("", "")
    
    # If first number lacks unit, inherit from later number (e.g. "12-18 LPA")

    unit = next((u for _, u in reversed(nums) if u), "")
    values = [n * _SALARY_UNITS.get(u or unit, 1) for n, u in nums[:2]]

    # Bare small numbers in Indian JDs mean LPA
    if not unit and not is_monthly and max(values) < 200:
        values = [v * 100000 for v in values]

    if is_monthly:
        values = [v * 12 for v in values]

    if len(values) == 1:
        lo, hi = values[0], values[0]
    else:
        lo, hi = sorted(values[:2])
    
    if hi <= 0:
        return ("", "")
    return (str(round(lo)), str(round(hi)))
    

def parse_experience(text: str | None) -> tuple[int | str, int | str]:
    """Parses experience text into (minExperienceYears, maxExperienceYears) as integers or ('', '').
    '4-8' -> (4, 8), '5+' -> (5, ''), reversed '8 to 4' -> (4, 8).
    """
    if not text:
        return ("", "")
    nums = [float(n) for n in re.findall(r"\d+(?:\.\d+)?", str(text))]
    if not nums:
        return ("", "")
    if len(nums) >= 2:
        lo, hi = sorted([int(round(nums[0])), int(round(nums[1]))])
        return (lo, hi)
    return (int(round(nums[0])), "")


def _clean_str(value: any) -> str:
    return " ".join(str(value).split()) if value not in (None, "") else ""

def _clean_list(items: any) -> list[str]:
    if not items:
        return []
    if isinstance(items, str):
        items = re.split(r"[,;\n]", items)
    out = []
    seen = set()
    for item in items:
        s = _clean_str(item)
        if s and s.lower() not in seen:
            seen.add(s.lower())
            out.append(s)
    return out

def _clean_job_type(val: any) -> str:
    v = _clean_str(val).lower()
    for pattern, kind in _JOB_TYPES:
        if pattern in v:
            return kind
    return ""

def _clean_work_mode(val: any) -> str:
    v = _clean_str(val).lower()
    for pattern, mode in _WORK_MODES:
        if pattern in v:
            return mode
    return ""

def _clean_priority(val: any) -> str:
    v = _clean_str(val).lower()
    if "high" in v or "urgent" in v:
        return "High"
    if "medium" in v:
        return "Medium"
    if "low" in v:
        return "Low"
    return ""

def _clean_notice(val: any) -> str:
    v = _clean_str(val).lower()
    if not v:
        return ""
    if "immediate" in v:
        return "0"
    m_days = re.search(r"(\d+)\s*days?", v)
    if m_days:
        return str(round(int(m_days.group(1)) / 30))
    m = re.search(r"\d+", v)
    return m.group(0) if m else ""

def _clean_openings(val: any) -> int | str:
    if isinstance(val, int) and val > 0:
        return val
    m = re.search(r"\d+", _clean_str(val))
    return int(m.group(0)) if m and int(m.group(0)) > 0 else ""

def _clean_date(val: any) -> str:
    v = _clean_str(val)[:10]
    try:
        datetime.strptime(v, "%Y-%m-%d")
        return v
    except ValueError:
        return ""

def _clean_digits(val: any) -> str:
    m = re.search(r"\d+", _clean_str(val))
    return m.group(0) if m else ""

def fix_jd(parsed: dict, today: date) -> dict:
    """Cleans, normalizes, and shapes the parsed JD into exactly the 30 output fields."""
    p = parsed or {}
    min_exp, max_exp = parse_experience(p.get("experienceAsWritten"))
    min_sal, max_sal = parse_salary(p.get("salaryAsWritten"))
    primary_skills = _clean_list(p.get("primarySkills"))
    skills_required = _clean_list(p.get("skillsRequired"))
    # Every primary skill is also included in skillsRequired
    req_seen = {s.lower() for s in skills_required}
    for skill in primary_skills:
        if skill.lower() not in req_seen:
            req_seen.add(skill.lower())
            skills_required.append(skill)

    return {
        "jobPositionTitle": _clean_str(p.get("jobPositionTitle")),
        "jobSummary": _clean_str(p.get("jobSummary")),
        "comments": _clean_str(p.get("comments")),
        "customerName": _clean_str(p.get("customerName")),
        "country": _clean_str(p.get("country")),
        "state": _clean_str(p.get("state")),
        "city": _clean_str(p.get("city")),
        "workAddress": _clean_str(p.get("workAddress")),
        "industry": _clean_str(p.get("industry")),
        "domain": _clean_str(p.get("domain")),
        "minExperienceYears": min_exp,
        "maxExperienceYears": max_exp,
        "minimumSalary": min_sal,
        "maximumSalary": max_sal,
        "primarySkills": primary_skills,
        "skillsRequired": skills_required,
        "skillDomains": _clean_list(p.get("skillDomains")),
        "degree": _clean_list(p.get("degree")),
        "languages": _clean_list(p.get("languages")),
        "jobType": _clean_job_type(p.get("jobType")),
        "workMode": _clean_work_mode(p.get("workMode")),
        "noticePeriodMonths": _clean_notice(p.get("noticePeriodMonths")),
        "numberOfOpenings": _clean_openings(p.get("numberOfOpenings")),
        "closingDate": _clean_date(p.get("closingDate")),
        "jobDurationMonths": _clean_digits(p.get("jobDurationMonths")),
        "jobWorkHoursPerDay": _clean_digits(p.get("jobWorkHoursPerDay")),
        "priority": _clean_priority(p.get("priority")),
        "postingDate": today.isoformat(),
        "jobStatus": "Active",
        "isOpenForReferral": False,
    }
