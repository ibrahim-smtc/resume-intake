"""TEMPORARY resume parser that uses Perfox's extract_structured instead of Questlight's parsing service.

Why it exists: Questlight's dev parser stopped answering at times (Cloudflare 524), and the plan is to compare it with
Perfox's before choosing one. It returns the same JSON shape as Questlight's parser, so the pipeline and the page don't
care which one produced it. Switch with RESUME_PARSER=questlight|perfox and a restart.

WARNING: the Perfox workspace this calls (PERFOX_BASE_URL) may belong to another client. Use FAKE test resumes only,
never real candidate data.

API key: PERFOX_API_KEY. It needs the documents:extract scope.
"""
import json
import os
import zipfile
from datetime import date

import httpx

from app.intake.documents import docx_text
from app.observability import tracing

TIMEOUT_S = 90  # one extraction took 10-19 s in testing


def _obj(props: dict) -> dict:
    # Perfox leaves out fields that aren't marked required, so every field is.
    return {"type": "object", "properties": props, "required": list(props), "additionalProperties": False}


def _str(description: str = "") -> dict:
    return {"type": "string", "description": description}


def _arr(items: dict, description: str = "") -> dict:
    return {"type": "array", "items": items, "description": description}


def _schema() -> dict:
    """Same shape as Questlight's parsing-ms output."""
    months_years = _obj({"years": {"type": "integer"}, "months": {"type": "integer"}})
    return _obj({
        "name": _str("Candidate's full name"),
        "email": _str("Email address exactly as written"),
        "phoneNumber": _str("Phone number exactly as written"),
        "address": _str("City and state/country as written"),
        "dateOfBirth": _str("YYYY-MM-DD or empty string"),
        "linkedInProfile": _str("LinkedIn URL or empty string"),
        "github": _str("GitHub URL or empty string"),
        "portfolio": _str("Portfolio/website URL or empty string"),
        "technical_skills": _arr(_str(), "Programming languages, frameworks, tools, libraries, databases, and cloud technologies. Do not duplicate course titles or certifications here."),
        "soft_skills": _arr(_str(), "Professional workplace interpersonal competencies only. Exclude hobbies, sports, and physical activities."),
        "total_experience": months_years,
        "workExperience": _arr(_obj({
            "companyName": _str(), "jobTitle": _str(), "location": _str(),
            "startDate": _str("YYYY-MM-DD"),
            "endDate": _str("YYYY-MM-DD. For ongoing roles (Present/Current/Till date), use today's date."),
            "isCurrent": {"type": "boolean", "description": "true if the candidate currently works in this role (Present, Current, Till date, or ongoing)"},
            "duration": months_years, "responsibilities": _arr(_str()),
        }), "Jobs, most recent first"),
        "education": _arr(_obj({
            "degree": _str(), "institution": _str(),
            "graduation_year": {"type": "integer", "description": "Year completed. If no graduation year is explicitly stated in the resume, output 0. Do not guess."}
        })),
        "certifications": _arr(_obj({
            "certificationName": _str(), "institution": _str(), "dateObtained": _str("YYYY-MM-DD or empty string"),
        })),
    })


def _prompt(today: date) -> str:
    return (
        "You are an accurate, strict resume parser. Extract information strictly as written in the document.\n"
        f"Context: Today's date is {today.isoformat()}.\n"
        "Universal Extraction Rules:\n"
        f"1. Ongoing Roles: For any job where the end date is 'Present', 'Current', 'Till date', or ongoing, set isCurrent=true and endDate='{today.isoformat()}'. Never set endDate to the start month.\n"
        "2. No Hallucinations: If any field (such as graduation year, date of birth, portfolio) is not explicitly stated in the document, use 0 for numbers or empty strings for text. Never guess or fabricate information.\n"
        "3. Skills vs Certifications: List programming languages, frameworks, cloud tools, databases, and developer libraries under 'technical_skills'. Put course titles and certificates under 'certifications'. Do not duplicate certification names into technical_skills.\n"
        "4. Soft Skills: Extract professional workplace competencies only (e.g., Leadership, Communication, Problem Solving). Exclude hobbies, sports, or physical activities."
    )


def _to_date(value, today: date):
    try:
        d = date.fromisoformat(str(value)[:10])
    except ValueError:
        return None
    return min(d, today)  # an end date can't be in the future


def _years_months(days: int) -> dict:
    months = round(days / 30.4375)
    return {"years": months // 12, "months": months % 12}


def _fix_experience(parsed: dict, today: date) -> None:
    """Computes accurate durations, resolves ongoing jobs, and cleans cross-category duplication."""
    # 1. Clean taxonomy: deduplicate certification names from technical skills
    cert_names = {
        cert.get("certificationName", "").strip().lower()
        for cert in (parsed.get("certifications") or [])
        if cert.get("certificationName")
    }
    if cert_names and parsed.get("technical_skills"):
        parsed["technical_skills"] = [
            s for s in parsed["technical_skills"]
            if isinstance(s, str) and s.strip().lower() not in cert_names
        ]

    # 2. Fix ongoing jobs and calculate durations
    spans = []
    for job in parsed.get("workExperience") or []:
        start = _to_date(job.get("startDate"), today)
        if job.get("isCurrent"):
            end = today
            job["endDate"] = today.isoformat()
        else:
            end = _to_date(job.get("endDate"), today) if job.get("endDate") else today

        if start and end and end >= start:
            job["duration"] = _years_months((end - start).days)
            spans.append((start, end))

    spans.sort()
    merged = []  # overlapping jobs are only counted once
    for start, end in spans:
        if merged and start <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    parsed["total_experience"] = _years_months(sum((e - s).days for s, e in merged))



async def parse_resume(name: str, data: bytes, ext: str):
    """Returns (parsed JSON, None) or (None, error message)."""
    base_url = os.getenv("PERFOX_BASE_URL", "").strip().rstrip("/")
    if not base_url:
        return None, "PERFOX_BASE_URL is not set (see .env.example)"
    key = os.getenv("PERFOX_API_KEY", "").strip()
    if key.lower().startswith("bearer "):
        key = key[7:]
    if not key:
        return None, "PERFOX_API_KEY is not set (see .env.example)"
    today = date.today()

    if ext == ".docx":
        # Perfox's own DOCX reading failed with a 500 on every try, so send the text instead.
        try:
            data = docx_text(data).encode("utf-8")
        except (zipfile.BadZipFile, KeyError):
            return None, "couldn't open this DOCX file (corrupt?)"
        name, mime = name + ".txt", "text/plain"
    else:
        mime = "application/pdf"

    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(TIMEOUT_S)) as client:
            resp = await client.post(
                f"{base_url}/documents/extract_structured",
                headers={"Authorization": f"Bearer {key}"},
                files={"file": (name, data, mime)},
                data={"schema": json.dumps(_schema()), "prompt": _prompt(today)},
            )
    except httpx.TimeoutException:
        return None, f"Perfox extraction timed out after {TIMEOUT_S}s"
    except httpx.HTTPError as exc:
        return None, f"couldn't reach Perfox ({type(exc).__name__}: {exc or 'no detail'})"

    tracing.current().set(endpoint="perfox documents/extract_structured", http_status=resp.status_code,
                          sent_as=mime, sent_kb=round(len(data) / 1024, 1))
    if resp.status_code != 200:
        try:
            body = resp.json()
            detail = f"{body.get('error')}: {body.get('message')}"
        except ValueError:
            detail = f"HTTP {resp.status_code}"
        return None, f"Perfox rejected the request ({detail})"

    try:
        body = resp.json()
        parsed = body["data"]
    except (ValueError, KeyError, TypeError):
        return None, "Perfox returned an unexpected response"
    usage = body.get("usage") or {}  # Perfox reports tokens but not which model it used
    if usage:
        tracing.record_usage(usage.get("input_tokens"), usage.get("output_tokens"), provider="perfox")
    else:
        tracing.current().set(tokens="not reported by Perfox")

    _fix_experience(parsed, today)
    return parsed, None
