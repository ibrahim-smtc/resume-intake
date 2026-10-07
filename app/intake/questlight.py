"""The "Load into Questlight" block: turns the parsed resume into a Questlight candidate profile.

Calls POST {QUESTLIGHT_BASE_URL}/applicants/create-applicant (multipart: applicantData JSON + the resume file), and
POST /jobs/applicantMatching/create to put the new candidate on a job at the Screening stage (add_to_screening).
Hardcoded rules only, no model. The API needs a Bearer JWT with the PROFILE:CREATE permission; see .env.example.

Required by the API: name, email, skills (non-empty), workExperience and education. When any is missing the
profile is NOT created and the missing fields are reported, so a later step can ask the candidate for them.
"""
import json
import os
import re
from datetime import date

import httpx

from app.observability import tracing

TIMEOUT_S = 60
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[A-Za-z]{2,}$")
MIME = {".pdf": "application/pdf",
        ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document"}


def _clean(value):
    """Drops empty strings, empty lists and nulls (the parser fills unknown fields with ''), keeps 0."""
    if isinstance(value, dict):
        cleaned = {k: _clean(v) for k, v in value.items()}
        return {k: v for k, v in cleaned.items() if v not in ("", None, [], {})}
    if isinstance(value, list):
        return [v for v in (_clean(v) for v in value) if v not in ("", None, [], {})]
    return value.strip() if isinstance(value, str) else value


def _valid_date(value) -> bool:
    try:
        date.fromisoformat(str(value)[:10])
        return True
    except ValueError:
        return False


TEXT_LIMIT = 100  # Questlight rejects (HTTP 400) jobTitle, companyName, degree, institution, certificationName over this
MISSING = "Not specified"  # jobTitle, companyName, degree and institution may not be empty; a resume that leaves one out gets this


def _fit(value, label: str, notes: list) -> str:
    """A text field that fits Questlight's rules: whitespace tidied and at most TEXT_LIMIT characters.
    Every change is noted (field name only, never the value) so it shows up on the trace."""
    text = " ".join(str(value).split()) if value is not None else ""
    if len(text) > TEXT_LIMIT:
        notes.append(f"{label} cut to {TEXT_LIMIT} characters")
        text = text[:TEXT_LIMIT].rstrip()
    return text


def _need(value, label: str, kind: str, index: int, who: str, changes: list, gaps: list, fill: bool) -> str:
    """A text field Questlight will not accept empty (job title, company name, degree, institution).
    When the resume leaves it out: with fill=False it is reported in `gaps` (id, what, who) so the recruiter can supply it,
    and the placeholder only keeps the payload well-formed (it is never sent while there are gaps); with fill=True the
    placeholder IS the value and the change is noted."""
    text = _fit(value, label, changes)
    if text:
        return text
    if fill:
        changes.append(f"{label} was empty")
    else:
        gaps.append({"id": f"{kind}:{index}", "what": label, "for": who})
    return MISSING


def _lines(value, notes: list) -> list:
    """responsibilities has to be a list of strings. A parser that returns one block of text gets it split at line breaks
    and bullet marks."""
    if isinstance(value, str):
        notes.append("responsibilities were text, made a list")
        value = re.split(r"[\r\n]+|\s*[•●▪‣◦]\s*", value)
    return [" ".join(str(item).split()) for item in value if str(item).strip()] if isinstance(value, list) else []


def _experience(value):
    """total_experience and a job's duration have to be {"years": n, "months": n} with whole numbers of at least 0.
    Anything else (a text like "5 years", negative or missing numbers) is left out rather than rejected."""
    if not isinstance(value, dict):
        return None
    out = {}
    for key in ("years", "months"):
        try:
            number = int(round(float(value.get(key) or 0)))
        except (TypeError, ValueError):
            return None
        out[key] = max(0, number)
    return out


def _year(value):
    """graduation_year has to be a number: takes 2019 from 2019, "2019" or "May 2019"; None when there is no year."""
    match = re.search(r"(?:19|20)\d{2}", str(value if value is not None else ""))
    return int(match.group()) if match else None


def build_applicant(parsed: dict, adjusted: list = None, gaps: list = None, fill: bool = False, source: str = "manual upload") -> dict:
    """Maps the parser's JSON onto Questlight's CreateApplicantDto. Only fields the DTO has are sent, and values are
    fitted to its rules (see _fit). What had to be adjusted is put on the current trace span and, when a list is passed
    as `adjusted`, added to it (field names only, never values), so the caller can tell the recruiter.
    Details Questlight requires but the resume lacks (see _need) go to `gaps`, or are filled with "Not specified" when
    fill=True. `source` is the intake channel, written into the profile's notes (the DTO has no source field).
    Prefer review(), which also works out the complete list of what is missing."""
    gaps = gaps if gaps is not None else []
    skills, seen = [], set()
    for s in (parsed.get("skills") or []) + (parsed.get("technical_skills") or []) + (parsed.get("soft_skills") or []):
        if isinstance(s, str) and s.strip() and s.strip().lower() not in seen:
            seen.add(s.strip().lower())
            skills.append(s.strip())

    # Questlight drops work entries without a start date, so they are not sent. i = the entry's place in the parsed list.
    jobs = [(i, {k: j.get(k) for k in ("jobTitle", "companyName", "startDate", "endDate", "responsibilities", "duration")})
            for i, j in enumerate(parsed.get("workExperience") or []) if _valid_date(j.get("startDate"))]
    for _, j in jobs:
        if not _valid_date(j.get("endDate")):
            j.pop("endDate", None)
    education = [(i, {k: e.get(k) for k in ("degree", "institution", "graduation_year")})
                 for i, e in enumerate(parsed.get("education") or []) if e.get("degree") or e.get("institution")]
    certs = [{k: c.get(k) for k in ("certificationName", "institution", "dateObtained")}
             for c in parsed.get("certifications") or [] if c.get("certificationName")]
    for c in certs:
        if not _valid_date(c.get("dateObtained")):
            c.pop("dateObtained", None)

    changes = []
    for i, j in jobs:
        company = " ".join(str(j.get("companyName") or "").split())[:60]
        title = " ".join(str(j.get("jobTitle") or "").split())[:60]
        start = str(j["startDate"])[:10]
        job = f"the job at {company}" if company else f"the job starting {start}"
        j["jobTitle"] = _need(j.get("jobTitle"), "a job title", "job_title", i, job, changes, gaps, fill)
        j["companyName"] = _need(j.get("companyName"), "a company name", "job_company", i,
                                 f"the {title} job starting {start}" if title else job, changes, gaps, fill)
        j["responsibilities"] = _lines(j.get("responsibilities"), changes)
        j["duration"] = _experience(j.get("duration"))
    for i, e in education:
        institution = " ".join(str(e.get("institution") or "").split())[:60]
        degree = " ".join(str(e.get("degree") or "").split())[:60]
        e["degree"] = _need(e.get("degree"), "a degree", "degree", i,
                            f"the education at {institution}" if institution else "an education entry", changes, gaps, fill)
        e["institution"] = _need(e.get("institution"), "an institution", "institution", i,
                                 f"the {degree} education" if degree else "an education entry", changes, gaps, fill)
        e["graduation_year"] = _year(e.get("graduation_year"))
    for c in certs:
        c["certificationName"] = _fit(c.get("certificationName"), "a certification name", changes)
        if c.get("institution") is not None:
            c["institution"] = _fit(c.get("institution"), "a certification's institution", changes)
    changes = sorted(set(changes))
    if changes:
        tracing.current().set(adjusted_for_questlight=changes)
        if adjusted is not None:
            adjusted.extend(changes)

    notes = [f"Source: resume intake, {source}"]  # the DTO has no source-channel field
    if parsed.get("github"):
        notes.insert(0, f"GitHub: {parsed['github']}")  # nor a GitHub field

    applicant = {
        "name": parsed.get("name"), "email": parsed.get("email"), "phoneNumber": parsed.get("phoneNumber"),
        "address": parsed.get("address"), "linkedInProfile": parsed.get("linkedInProfile"),
        "portfolio": parsed.get("portfolio"),
        "dateOfBirth": parsed.get("dateOfBirth") if _valid_date(parsed.get("dateOfBirth")) else None,
        "skills": skills, "total_experience": _experience(parsed.get("total_experience")),
        "workExperience": [j for _, j in jobs], "education": [e for _, e in education], "certifications": certs,
        "additionalNotes": "; ".join(notes)[:1000],
    }
    cleaned = _clean(applicant)
    for required_list in ("skills", "workExperience", "education"):
        cleaned.setdefault(required_list, [])  # the API wants the arrays present even when empty
    return cleaned


def unreadable_items(message: str) -> list:
    """When Questlight's parser returns no JSON at all because it could not find the candidate's name, email or phone
    (its 422 message: "We couldn't find the following details: email"), the items to ask the recruiter for."""
    # only the first sentence names the details; the next one ("...clearly shows the name, email, and phone number") must not count
    words = set(re.findall(r"[a-z]+", str(message or "").split(":", 1)[-1].split(".")[0].lower()))
    ids = [k for k, found in (("name", "name" in words), ("email", "email" in words),
                              ("phone", any(w.startswith("phone") for w in words))) if found] or ["name", "email"]
    return [{"id": k, "what": k, "for": "the candidate", "can_supply": True} for k in ids]


def unreadable_note(message: str) -> str:
    """The note that goes with a resume the parser stopped on: what it could not find, and that the rest was not read yet.
    (Questlight's own sentence is not passed on: it lists name, email and phone again, which reads as all three missing.)"""
    what = " and ".join(i["what"] for i in unreadable_items(message))
    return (f"The resume does not show the candidate's {what}, so Questlight's parser read nothing from it yet. "
            "Nothing else is known to be missing: once these are supplied the resume is read again.")


def review(parsed: dict, fill: bool = False, source: str = "manual upload") -> dict:
    """Everything the loader needs to know about a parsed resume:
    applicant (the payload), missing (readable list of what blocks the profile, e.g. "a job title for the job at Acme"),
    items (the same gaps as {id, what, for}: the ones with a "kind:index" id can be filled in by a recruiter),
    adjusted (what was filled in or shortened). With fill=True the gaps are filled with "Not specified" instead."""
    adjusted, gaps = [], []
    applicant = build_applicant(parsed, adjusted, gaps, fill, source)
    basics = missing_required(applicant)
    # name, email and skills can be typed in by the recruiter; work history and education need a resume that shows them
    items = [{"id": b, "what": b, "for": "the candidate", "can_supply": b in ("name", "email", "skills")} for b in basics]
    gaps = [dict(g, can_supply=True) for g in gaps]
    return {"applicant": applicant, "adjusted": adjusted, "items": items + gaps,
            "missing": basics + [f"{g['what']} for {g['for']}" for g in gaps]}


def missing_required(applicant: dict) -> list:
    """Fields the API requires that this resume doesn't have. Empty list means the profile can be created."""
    missing = []
    if not str(applicant.get("name") or "").strip():
        missing.append("name")
    if not EMAIL_RE.match(str(applicant.get("email") or "")):
        missing.append("email")
    for field in ("skills", "workExperience", "education"):
        if not applicant.get(field):
            missing.append(field)
    return missing


def missing_recommended(applicant: dict) -> list:
    """Not needed to create the profile, but recruiters need it to reach the candidate."""
    return [] if sum(c.isdigit() for c in str(applicant.get("phoneNumber") or "")) >= 10 else ["phoneNumber"]


def _field_errors(body) -> list:
    """Questlight's validation failures. Its top-level "message" is useless ("[object Object]; [object Object]...");
    the real reasons are in errorMessage.response.errors, each {"field", "message"} (they name fields, never values)."""
    errors = None
    if isinstance(body, dict):
        inner = body.get("errorMessage")
        errors = (inner.get("response") or {}).get("errors") if isinstance(inner, dict) and isinstance(inner.get("response"), dict) else None
        errors = errors or body.get("errors")
    out = []
    for e in errors if isinstance(errors, list) else []:
        text = e.get("message") if isinstance(e, dict) else (e if isinstance(e, str) else None)
        if text and text not in out:
            out.append(text)
    return out


def _problem(resp: httpx.Response) -> str:
    try:
        body = resp.json()
    except ValueError:
        return f"HTTP {resp.status_code}"
    found = _field_errors(body)
    if found:
        return "; ".join(found)[:600]
    msg = body.get("message") or body.get("errorMessage") or body.get("errors") if isinstance(body, dict) else None
    return msg if isinstance(msg, str) else json.dumps(msg)[:300]


def api_base() -> str:
    return os.getenv("QUESTLIGHT_BASE_URL", "https://dev-api.quest-light.com/api").rstrip("/")


def auth_headers():
    """Headers for Questlight's core API, or None when QUESTLIGHT_TOKEN isn't set."""
    token = os.getenv("QUESTLIGHT_TOKEN", "").strip()
    if token.lower().startswith("bearer "):
        token = token[7:]
    if not token:
        return None
    headers = {"Authorization": f"Bearer {token}"}
    if os.getenv("QUESTLIGHT_ORIGIN"):  # TenantOriginGuard compares this with the tenant's allowed domains
        headers["Origin"] = os.getenv("QUESTLIGHT_ORIGIN")
    return headers


async def create_profile(applicant: dict, name: str, data: bytes, ext: str) -> dict:
    """Returns {"status": created | duplicate | skipped | failed, "message": ..., plus the IDs when created}."""
    headers = auth_headers()
    if not headers:
        return {"status": "skipped", "message": "QUESTLIGHT_TOKEN is not set, so no profile was created"}

    parts = [("applicantData", (None, json.dumps(applicant)))]  # (None, ...) makes it a plain multipart field
    attach = os.getenv("QUESTLIGHT_ATTACH_RESUME", "true").lower() != "false"
    if attach:
        parts.append(("resume", (name, data, MIME[ext])))

    with tracing.span("POST create-applicant") as s:
        s.set(resume_attached=attach)
        result = await _send_profile(headers, parts)
        s.set(outcome=result["status"])
        if result["status"] == "failed":
            s.fail(result["message"])
    return result


# A candidate whose overall status is one of these gets SCREENING once added to a job, as Questlight's own "add to
# pipeline" does. Anyone further along (submitted to a client, interviewing...) keeps their status: it is never lowered.
RAISE_TO_SCREENING = {"", "NEW_CANDIDATE"}


async def add_to_screening(applicant_id: str, job_id: str, current_status: str = "NEW_CANDIDATE") -> dict:
    """Puts a candidate on a job at the SCREENING stage (status ONGOING), the way Questlight's UI does: POST
    /jobs/applicantMatching/create, then POST /applicants/change-status for a new candidate. Both IDs are the long UUIDs
    (the `_id` fields), not the readable CAN-... / JOB-... codes.
    Returns {"status": screened | already | skipped | failed, "message", "status_updated"}."""
    headers = auth_headers()
    if not headers:
        return {"status": "skipped", "message": "QUESTLIGHT_TOKEN is not set"}
    body = {"applicantId": applicant_id, "jobId": job_id, "stages": "SCREENING", "status": "ONGOING"}
    with tracing.span("POST applicantMatching/create") as s:
        result = await _send_screening(headers, body)
        s.set(outcome=result["status"])
        if result["status"] == "failed":
            s.fail(result["message"])
    if result["status"] == "screened" and (current_status or "") in RAISE_TO_SCREENING:
        result["status_updated"] = await _set_status(headers, applicant_id, "SCREENING")
    return result


async def _set_status(headers: dict, applicant_id: str, status: str) -> bool:
    """POST /applicants/change-status. Failing here doesn't undo the screening, so it only reports whether it worked."""
    with tracing.span("POST applicants/change-status") as s:
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(TIMEOUT_S)) as client:
                resp = await client.post(f"{api_base()}/applicants/change-status", headers=headers,
                                         json={"applicant_id": applicant_id, "status": status,
                                               "remarks": "Added to screening by the resume intake assistant"})
        except httpx.HTTPError as exc:
            s.fail(type(exc).__name__)
            return False
        s.set(http_status=resp.status_code)
        if resp.status_code not in (200, 201):
            s.fail(f"HTTP {resp.status_code}")
            return False
        return True


async def _send_screening(headers: dict, body: dict) -> dict:
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(TIMEOUT_S)) as client:
            resp = await client.post(f"{api_base()}/jobs/applicantMatching/create", headers=headers, json=body)
    except httpx.TimeoutException:
        return {"status": "failed", "message": f"Questlight didn't answer within {TIMEOUT_S}s, so it is unknown whether this went through"}
    except httpx.HTTPError as exc:
        return {"status": "failed", "message": f"couldn't reach Questlight ({type(exc).__name__}: {exc or 'no detail'})"}
    code = resp.status_code
    tracing.current().set(http_status=code)
    if code in (200, 201):
        return {"status": "screened", "message": "added to the job at the Screening stage"}
    problem = _problem(resp)
    # "Applicant has already been mapped for this job" comes as a 400; a leftover scorecard for the pair as a 409.
    if code == 409 or (code == 400 and "already" in problem.lower()):
        return {"status": "already", "message": "the candidate is already on this job"}
    if code == 422:
        return {"status": "failed", "message": "Questlight can't screen for this job yet: it has no screening questions at this "
                "candidate's experience level (Questlight 422)"}
    if code == 401:
        return {"status": "failed", "message": "Questlight rejected the token (tokens last 10 days): get a new one and update QUESTLIGHT_TOKEN"}
    if code == 403:
        return {"status": "failed", "message": "Questlight refused: the account may not add candidates to jobs, or this app's origin isn't allowed (try QUESTLIGHT_ORIGIN)"}
    return {"status": "failed", "message": f"Questlight returned HTTP {code}: {problem}"}


async def _send_profile(headers: dict, parts: list) -> dict:
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(TIMEOUT_S)) as client:
            resp = await client.post(f"{api_base()}/applicants/create-applicant", headers=headers, files=parts)
    except httpx.TimeoutException:
        return {"status": "failed", "message": f"Questlight didn't answer within {TIMEOUT_S}s, no profile was confirmed"}
    except httpx.HTTPError as exc:
        return {"status": "failed", "message": f"couldn't reach Questlight ({type(exc).__name__}: {exc or 'no detail'})"}

    code = resp.status_code
    tracing.current().set(http_status=code)
    if code == 201:
        try:
            saved = resp.json()["data"]
            saved = saved.get("savedApplicant", saved)
            return {"status": "created", "message": "profile created in Questlight",
                    "applicantId": saved.get("applicantId"), "id": saved.get("_id")}
        except (ValueError, KeyError, AttributeError):
            return {"status": "created", "message": "profile created in Questlight (response had no ID)"}
    if code == 409:
        return {"status": "duplicate", "message": "a profile with this email already exists in Questlight, nothing was changed"}
    if code == 401:
        return {"status": "failed", "message": "Questlight rejected the token (tokens last 10 days): get a new one and update QUESTLIGHT_TOKEN"}
    if code == 403:
        return {"status": "failed", "message": "Questlight refused: the account needs the PROFILE:CREATE permission, "
                "or this app's origin isn't allowed for the tenant (try setting QUESTLIGHT_ORIGIN)"}
    return {"status": "failed", "message": f"Questlight returned HTTP {code}: {_problem(resp)}"}
