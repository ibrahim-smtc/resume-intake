"""perfox-parsers/schema.py
Schema and prompt management for Perfox structured resume extraction.
"""
from datetime import date
from pathlib import Path

# ── 1. Prompt Loader ─────────────────────────────────────────────────────────
# Read prompt.md once at import time

_PROMPT_PATH = Path(__file__).parent / "prompt.md"
_PROMPT_TEMPLATE = _PROMPT_PATH.read_text(encoding="utf-8")

def build_prompt(today: date) -> str:
    """ format prompts template with today's dynamic ISO date"""
    return _PROMPT_TEMPLATE.format(today = today.isoformat())

# ── 2. JSON Schema Helpers ───────────────────────────────────────────────────
def _obj(props: dict) -> dict:
    """Critical: Perfox silently drops fields not listed in `required`.
    Every property defined here is automatically added to `required`.
    """
    return {
        "type": "object",
        "properties": props,
        "required": list(props),
        "additionalProperties": False,
    }

def _str (description: str = "") -> dict:
    return {
        "type": "string",
        "description": description
    }

def _arr(items: dict, description: str = "") -> dict:
    return {
        "type": "array", "items": items, "description": description
    }

# ── 3. Schema Definition ─────────────────────────────────────────────────────
def build_schema() -> dict:
    """Returns JSON schema matching Questlight's parsing-ms output structure."""
    months_years = _obj({
        "years": {"type": "integer"},
        "months": {"type": "integer"}
    })

    return _obj({
        "name": _str("Candidate's full name"),
        "email": _str("Email address exactly as written"),
        "phoneNumber": _str("Phone number exactly as written"),
        "address": _str("City and state/country as written"),
        "dateOfBirth": _str("YYYY-MM-DD or empty string"),
        "linkedInProfile": _str("LinkedIn URL or empty string"),
        "github": _str("GitHub URL or empty string"),
        "portfolio": _str("Portfolio/website URL or empty string"),
        "technical_skills": _arr(
            _str(),
            "Programming languages, frameworks, tools, libraries, databases, and cloud technologies. "
            "Do not duplicate course titles or certifications here."
        ),
        "soft_skills": _arr(
            _str(),
            "Professional workplace interpersonal competencies only. Exclude hobbies, sports, and physical activities."
        ),
        "total_experience": months_years,
        "workExperience": _arr(_obj({
            "companyName": _str(),
            "jobTitle": _str(),
            "location": _str(),
            "startDate": _str("YYYY-MM-DD"),
            "endDate": _str("YYYY-MM-DD. For ongoing roles (Present/Current/Till date), use today's date."),
            "isCurrent": {
                "type": "boolean",
                "description": "true if the candidate currently works in this role (Present, Current, Till date, or ongoing)"
            },
            "duration": months_years,
            "responsibilities": _arr(_str()),
        }), "Jobs, most recent first"),
        "education": _arr(_obj({
            "degree": _str(),
            "institution": _str(),
            "graduation_year": {
                "type": "integer",
                "description": "Year completed. If no graduation year is explicitly stated in the resume, output 0. Do not guess."
            }
        })),
        "certifications": _arr(_obj({
            "certificationName": _str(),
            "institution": _str(),
            "dateObtained": _str("YYYY-MM-DD or empty string"),
        })),
    })

