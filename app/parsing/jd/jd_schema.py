"""app/parsing/jd/jd_schema.py
Schema definition for Perfox structured Job Description (JD) extraction.
"""
from datetime import date
from pathlib import Path
def _obj(props: dict) -> dict:
    return {
        "type": "object",
        "properties": props,
        "required": list(props),
        "additionalProperties": False,
    }


def _str(description: str = "") -> dict:
    return {"type": "string", "description": description}


def _arr(items: dict, description: str = "") -> dict:
    return {"type": "array", "items": items, "description": description}

# ── 1. Prompt Loader ─────────────────────────────────────────────────────────

_PROMPT_PATH = Path(__file__).parent / "jd_prompt.md"
_PROMPT_TEMPLATE = _PROMPT_PATH.read_text(encoding="utf-8")

def build_jd_prompt(today: date) -> str:
    """Format JD prompt template with today's dynamic ISO date."""
    return _PROMPT_TEMPLATE.format(today = today.isoformat())

# ── 2. Schema Definition ─────────────────────────────────────────────────────
def build_jd_schema() -> dict:
    """Returns JSON schema for Perfox JD extraction.
    Contains exactly 25 model-extracted properties.
    Every property is automatically placed in 'required' by _obj() so Perfox does not drop it.
    """
    return _obj({
        # Role & summary
        "jobPositionTitle": _str("Job title only, Title Case, no city or work mode"),
        "jobSummary": _str("One or two sentences summarizing the role"),
        "comments": _str("Extra preferences or nice-to-haves as one short paragraph"),
        "customerName": _str("The client or hiring company name, only if explicitly stated"),
        # Location
        "country": _str("Country as written"),
        "state": _str("State or province as written"),
        "city": _str("City as written"),
        "workAddress": _str("Full work address if mentioned"),
        # Classification (may be inferred from role)
        "industry": _str("Industry inferred from the role, e.g. IT, Banking"),
        "domain": _str("Domain inferred from the role, e.g. Software Testing, Backend Development"),
        # Skills & requirements
        "primarySkills": _arr(_str(), "Core must-have skills and tools named in title/requirements"),
        "skillsRequired": _arr(_str(), "Every named tool, tech, language or practice; includes primary skills"),
        "skillDomains": _arr(_str(), "Two to four broad technical areas, e.g. QA Automation, CI/CD"),
        "degree": _arr(_str(), "Educational qualifications, one item per degree"),
        "languages": _arr(_str(), "Programming or spoken languages named as required"),
        # Work terms
        "jobType": _str("Full-time, Part-time, Contract, or empty string"),
        "workMode": _str("Remote, Hybrid, On-site, or empty string"),
        "noticePeriodMonths": _str("Notice period in months as digits, e.g. 1 for 30 days, 0 for immediate"),
        "numberOfOpenings": _str("Number of openings as digits, or empty string if not stated"),
        "closingDate": _str("Closing date in YYYY-MM-DD format, or empty string"),
        "jobDurationMonths": _str("Contract duration in months as digits, or empty string"),
        "jobWorkHoursPerDay": _str("Working hours per day as digits, or empty string"),
        "priority": _str("High, Medium, Low, only if urgency is stated"),
        # Raw compensation & experience (copied verbatim from text, no math)
        "salaryAsWritten": _str("Salary text copied exactly as written, e.g. 12-18 LPA, 80k-1L"),
        "experienceAsWritten": _str("Experience text copied exactly as written, e.g. 4-8 years, 5+"),
    })
