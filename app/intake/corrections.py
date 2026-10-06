"""Details a recruiter supplies for what a resume lacked (a job title, say), written into the parsed resume.

Nothing here talks to Questlight: it only edits the parsed copy the server holds. The ids are the ones that
questlight.review() puts in missing_items: job_title:N, job_company:N, degree:N, institution:N (N = the entry's place in
the parsed list), and name, email, phone, address, skills.
"""
from app.intake import questlight

_ENTRY_FIELDS = {"job_title": ("workExperience", "jobTitle"), "job_company": ("workExperience", "companyName"),
                 "degree": ("education", "degree"), "institution": ("education", "institution")}
_TOP_FIELDS = {"name": "name", "phone": "phoneNumber", "address": "address"}


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
