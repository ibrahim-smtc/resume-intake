"""Shaping results for the agent: short, with decision flags and a few plain facts, never the full parsed resume."""
from app import pipeline, settings
from app.agent.intakes import Intake
from app.intake import questlight

TOP_SKILLS = 20  # skills listed back to the agent; the profile in Questlight gets all of them
UNREADABLE = "name, email or phone (the parser couldn't read them)"


def candidate(parsed):
    """A short summary of the candidate. `parsed` is the (redacted when MASKING_ENABLED=true) resume JSON."""
    if not parsed:
        return None
    exp = parsed.get("total_experience") or {}
    skills = [s for s in (parsed.get("technical_skills") or []) + (parsed.get("skills") or []) if isinstance(s, str)]
    return {"name": parsed.get("name"), "email": parsed.get("email"), "phone": parsed.get("phoneNumber"),
            "location": parsed.get("address"),
            "experience": f"{exp.get('years') or 0} years {exp.get('months') or 0} months" if exp else None,
            "recent_roles": [" at ".join(x for x in (j.get("jobTitle"), j.get("companyName")) if x)
                             for j in (parsed.get("workExperience") or [])[:3]],
            "education": [e.get("degree") or e.get("institution") for e in parsed.get("education") or []],
            "skills": skills[:TOP_SKILLS]}


def top_roles(roles):
    return [{"title": m["title"], "job_id": m["jobId"], "score": m["score"], "location": m["location"],
             "matched_skills": m["matchedSkills"][:8], "missing_skills": m["missingSkills"][:8],
             "experience": m["experience"]} for m in (roles or {}).get("matches") or []]


def profile_view(profile: dict) -> dict:
    return {"status": profile.get("status"), "message": profile.get("message"),
            "candidate_id": profile.get("applicantId"), "adjusted": profile.get("adjusted") or []}


def next_hint(status):
    """What the agent should do next, for a profile in this state (None when there is nothing more to do)."""
    if status == "failed":
        return "create_profile(file_id) retries the creation"
    if status in ("not_created", "skipped"):
        return ("ask the recruiter for each missing item, then provide_missing_details(file_id, answers) and "
                "create_profile(file_id); or create_profile(file_id, fill_missing=true) if they say to go ahead without it")
    return None


def for_agent(result: dict) -> dict:
    """Trims the pipeline's result to what the agent needs."""
    if not result["ok"]:
        return {"ok": False, "decision": "error", "file": result["file"], "error": result["error"],
                "profile_created": False}
    profile = result.get("profile") or {}
    roles = result.get("roles") or {}
    return {
        "ok": True, "file": result["file"], "parser": result.get("parser"),
        "decision": result["decision"], "reason": result["reason"],
        "candidate": candidate(result.get("redacted")),
        "profile_created": profile.get("status") == "created",
        "profile": profile_view(profile) if profile else None,
        "info_complete": not result.get("missing_fields"),
        "missing_required": result.get("missing_fields") or [],
        "missing_items": profile.get("missing_items") or [],
        "missing_recommended": result.get("missing_recommended") or [],
        "note": result.get("warning"),
        "roles_status": roles.get("status"), "roles_message": roles.get("message"), "top_roles": top_roles(roles),
        "trace_id": (result.get("trace") or {}).get("trace_id"),
    }


def status_of(intake: Intake) -> dict:
    """What process_resume answers for a file it already processed in this chat: where things stand now (including
    details supplied since), without parsing or writing anything again."""
    profile = intake.profile or {"status": "not_created", "message": "no profile has been created yet"}
    exists = profile.get("status") in ("created", "duplicate")
    checked = questlight.review(intake.parsed) if intake.parsed is not None else None
    if exists:
        missing, items = [], []
    elif checked:
        missing, items = checked["missing"], checked["items"]
    else:
        missing, items = [UNREADABLE], questlight.unreadable_items(intake.incomplete)
    roles = intake.roles or {}
    out = {"ok": True, "file": intake.name, "parser": settings.PARSER, "decision": "accepted",
           "reason": "this file was already processed in this chat, so nothing was run again",
           "candidate": candidate(pipeline.redact(intake.parsed)) if intake.parsed else None,
           "profile_created": profile.get("status") == "created", "profile": profile_view(profile),
           "info_complete": not missing, "missing_required": missing, "missing_items": items,
           "missing_recommended": questlight.missing_recommended(checked["applicant"]) if checked else [],
           "note": None, "roles_status": roles.get("status"), "roles_message": roles.get("message"),
           "top_roles": top_roles(roles), "file_id": intake.id}
    hint = next_hint(profile.get("status"))
    if hint:
        out["next"] = hint
    elif not roles:
        out["next"] = "match_roles(file_id) shows the best matching open roles"
    return out
