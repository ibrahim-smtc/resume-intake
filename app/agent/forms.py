"""Dynamic Pydantic fill-in forms (/fill/<token>) for collecting missing resume fields."""
from typing import Annotated

from pydantic import AfterValidator, ConfigDict, Field, ValidationError, create_model

from app import settings
from app.agent import formatting, intakes, notify
from app.intake import corrections, questlight

# What each detail is called on the form, which kind of input it gets, and how long it may be.
BASIC = {"name": ("Candidate's full name", "text", 100), "email": ("Candidate's email", "email", 254),
         "phone": ("Candidate's phone number", "tel", 30), "address": ("Location (city)", "text", 200),
         "skills": ("Skills, comma-separated", "skills", 1000)}
ENTRY = {"job_title": "Job title", "job_company": "Company name", "degree": "Degree", "institution": "Institution",
         "job_start": "Start month and year", "job_end": "End month and year"}
MONTH_HINT = "MM/YYYY, like 03/2020"
CORRECTABLE = ("name", "email", "phone", "address")  # shown filled in, to correct what the parser read
PARSED_KEY = {"name": "name", "email": "email", "phone": "phoneNumber", "address": "address"}
SHOWN_SKILLS = 12

# The form opens without the app's password (the token in the link is its key) and shows the candidate's details, so its
# answers are never cached, never sent on as a Referer, and never indexed.
PRIVATE = {"Cache-Control": "no-store", "Referrer-Policy": "no-referrer", "X-Robots-Tag": "noindex, nofollow"}
GONE = "This form is no longer open: forms last 30 minutes after the resume was sent. Send the resume to the agent again."


def link(intake) -> str:
    """The private link to this file's form. Relative when PUBLIC_BASE_URL is empty (the chat page opens it itself)."""
    return f"{settings.PUBLIC_BASE_URL}/fill/{intakes.form_token(intake)}"


# ---------- the fields, from where the file stands ----------

def _clean(value) -> str:
    return " ".join(str(value or "").split()) if isinstance(value, str) else ""


def _missing_field(item: dict) -> dict:
    kind, _, number = item["id"].partition(":")
    placeholder = ""
    if kind == "job_start":  # a job with no usable start date: its start (here) and its end (see _end_field) are asked together
        label, kind, size, placeholder = ENTRY[kind], "month", 20, MONTH_HINT
        hint = f"Missing: the start and end dates for {item['for']}"
    elif number:
        label, kind, size = ENTRY.get(kind, item["what"]), "text", 100
        hint = f"Missing: {item['what']} for {item['for']}"
    else:
        label, kind, size = BASIC.get(kind, (item["what"], "text", 200))
        hint = "Missing from the resume"
    return {"id": item["id"], "label": label, "hint": hint, "input": kind, "max": size, "group": "missing", "value": "",
            "placeholder": placeholder}


def _end_field(start: dict) -> dict:
    """The end of the job whose start is asked for: required too, so a finished job is never taken for a current one. Present is allowed."""
    who = start["hint"].partition(" for ")[2]  # "the Network Engineer job at Tikona"
    return {**start, "id": start["id"].replace("job_start", "job_end"), "label": ENTRY["job_end"], "input": "month_end",
            "placeholder": "MM/YYYY, or Present", "hint": f"The end of {who}. Type Present if they still work there" if who else "Type Present if they still work there"}


def fields(intake) -> tuple[list, list]:
    """(fields, blocked): what the form asks for, and what it can't ask for (a detail only a new resume can give)."""
    if intake.parsed is None:  # the parser stopped on missing contact details: only those can be given, then the resume is read
        asked = {"phone": "phoneNumber"}
        items = [i for i in questlight.unreadable_items(intake.incomplete) if not intake.contact.get(asked.get(i["id"], i["id"]))]
        return [_missing_field(i) for i in items], []
    checked = questlight.review(intake.parsed)
    out = []
    for i in checked["items"]:
        if i.get("can_supply"):
            out.append(_missing_field(i))
            if i["id"].startswith("job_start:"):
                out.append(_end_field(out[-1]))
    blocked = [f"{i['what']} ({i['for']})" for i in checked["items"] if not i.get("can_supply")]
    asked = {f["id"] for f in out}
    for key in CORRECTABLE:
        if key in asked:
            continue
        label, kind, size = BASIC[key]
        hidden = settings.MASKING and key in ("email", "phone")  # masking hides contact details: they aren't shown here either
        out.append({"id": key, "label": label, "input": kind, "max": size, "group": "read",
                    "hint": "Hidden. Leave empty to keep what was read" if hidden else "As read from the resume: correct it if it's wrong",
                    "value": "" if hidden else _clean(intake.parsed.get(PARSED_KEY[key]))})
    if "skills" not in asked:
        have = [s for s in (intake.parsed.get("technical_skills") or []) + (intake.parsed.get("skills") or []) if isinstance(s, str)]
        more = f" (and {len(have) - SHOWN_SKILLS} more)" if len(have) > SHOWN_SKILLS else ""
        out.append({"id": "skills", "label": "Add skills", "input": "skills", "max": 1000, "group": "read", "value": "",
                    "hint": f"Already read: {', '.join(have[:SHOWN_SKILLS])}{more}" if have else "None were read"})
    return out, blocked


# ---------- the model ----------

def _rule(kind: str, required: bool):
    def check(value: str) -> str:
        value = " ".join(value.split())
        if not value:
            if required:
                raise ValueError("this is needed")
            return value
        if kind == "email" and not questlight.EMAIL_RE.match(value):
            raise ValueError("that doesn't look like an email address")
        if kind == "tel" and sum(c.isdigit() for c in value) < 10:
            raise ValueError("a phone number needs at least 10 digits")
        if kind in ("month", "month_end"):
            problem = corrections.date_problem(value, kind == "month_end")
            if problem:
                raise ValueError(problem)
            got = corrections.parse_month(value)
            value = f"{got[1]:02d}/{got[0]}" if isinstance(got, tuple) else "Present"
        if kind == "skills":
            value = ", ".join(s.strip() for s in value.replace(";", ",").split(",") if s.strip())
        return value
    return check


def model(form_fields: list, skip: bool = False):
    """A Pydantic model for exactly these fields, made for this one request. Missing details are required, unless the
    recruiter chose to skip them; details that were read are optional and default to what was read."""
    spec = {}
    for n, f in enumerate(form_fields):
        required = f["group"] == "missing" and not skip
        rule = Annotated[str, AfterValidator(_rule(f["input"], required))]
        extra = {"group": f["group"], "input": f["input"], "placeholder": f.get("placeholder", "")}
        info = Field(alias=f["id"], title=f["label"], description=f["hint"], max_length=f["max"], json_schema_extra=extra)
        if not required:
            info = Field(f["value"], alias=f["id"], title=f["label"], description=f["hint"], max_length=f["max"], json_schema_extra=extra)
        spec[f"f{n}"] = (rule, info)
    return create_model("MissingDetails", __config__=ConfigDict(extra="forbid"), **spec)


def _errors(exc: ValidationError) -> dict:
    out = {}
    for e in exc.errors():
        key = str(e["loc"][0]) if e.get("loc") else "form"
        out.setdefault(key, e["msg"].removeprefix("Value error, "))
    return out


# ---------- what the page gets ----------

def _result(intake) -> dict:
    roles = intake.roles or {}
    total = (intake.parsed or {}).get("total_experience") or {}
    return {"file": intake.name, "name": _clean((intake.parsed or {}).get("name")), "profile": formatting.profile_view(intake.profile or {}),
            "experience": f"{total.get('years') or 0} years {total.get('months') or 0} months" if total else None,
            "roles_status": roles.get("status"), "roles_message": roles.get("message"),
            "top_roles": formatting.top_roles(roles), "screening": formatting.screening(roles)}


def view(intake) -> dict:
    """The form as it stands now. status "open" (something to fill in or correct) or "done" (the profile exists: the result)."""
    if (intake.profile or {}).get("status") in ("created", "duplicate"):
        return {"status": "done", "file": intake.name, "result": _result(intake)}
    form_fields, blocked = fields(intake)
    missing = [f for f in form_fields if f["group"] == "missing"]
    unread = intake.parsed is None
    if unread:
        intro = "The resume doesn't show these, so it couldn't be read yet. Once they're in, it is read again (about 15 seconds)."
    elif missing:
        intro = "Questlight needs these before it can create the profile."
    else:
        intro = "Nothing is missing. Check what was read, then create the profile."
    can_skip = not unread and not blocked and bool(missing) and all(":" in f["id"] for f in missing)
    dates = any(f["id"].startswith("job_start:") for f in missing)
    others = any(not f["id"].startswith(("job_start:", "job_end:")) for f in missing)
    out = {"status": "open", "file": intake.name, "title": "Complete the candidate's details", "intro": intro,
           "schema": model(form_fields).model_json_schema(), "blocked": blocked, "can_submit": not blocked, "can_skip": can_skip,
           "skip_note": ("Leaves the jobs with no dates off the profile" + (" and saves the other missing ones as \"Not specified\"." if others else ".")
                       if dates else "Saves the missing ones as \"Not specified\".") if can_skip else None}
    if blocked:
        out["intro"] = "This resume can't be completed here: a new resume that shows these is needed."
    elif missing and not can_skip and not unread:
        out["skip_note"] = "Can't be skipped: Questlight won't create a profile without " + ", ".join(f["label"].lower() for f in missing if ":" not in f["id"]) + "."
    return out


async def submit(intake, body: dict) -> tuple[int, dict]:
    """Checks the answers with the form's model, stores them and creates the profile, then matches open roles (and, for a
    new profile with a strong match, screens it onto the top jobs). Returns (HTTP status, what the page shows next)."""
    from app.agent import tools  # here, not at the top: tools.py hands out this module's links
    async with intake.form_lock:  # a double click waits, then finds the profile made
        current = view(intake)
        if current["status"] == "done":
            return 200, current
        skip = body.get("skip") is True
        if not current["can_submit"]:
            return 422, {**current, "message": "This resume needs a new version that shows " + ", ".join(current["blocked"]) + "."}
        if skip and not current["can_skip"]:
            return 422, {**current, "message": current["skip_note"] or "These details can't be skipped."}
        form_fields, _ = fields(intake)
        values = body.get("values") if isinstance(body.get("values"), dict) else {}
        try:
            clean = model(form_fields, skip).model_validate(values).model_dump(by_alias=True)
        except ValidationError as exc:
            return 422, {**current, "errors": _errors(exc), "message": "Please check the highlighted fields."}
        spans = {f["id"].replace("job_start", "job_end"): corrections.span_problem(clean.get(f["id"]), clean.get(f["id"].replace("job_start", "job_end")))
                 for f in form_fields if f["id"].startswith("job_start:")}
        if any(spans.values()):
            return 422, {**current, "errors": {k: v for k, v in spans.items() if v}, "message": "Please check the highlighted fields."}
        answers = [{"id": f["id"], "value": clean[f["id"]]} for f in form_fields
                   if clean.get(f["id"]) and (f["group"] == "missing" or clean[f["id"]] != f["value"])]
        if answers or intake.parsed is None:
            got = await tools.provide_missing_details(intake.id, answers)
            if got.get("rejected"):
                errors = dict(r.split(": ", 1) for r in got["rejected"] if ": " in r)
                return 422, {**view(intake), "errors": errors, "message": "Please check the highlighted fields."}
            if not got.get("ok"):
                return 200, {**view(intake), "message": got.get("error") or "That didn't work. Please try again."}
            if intake.parsed is None:  # still waiting for a contact detail
                return 200, {**view(intake), "message": "Saved. A few details are still needed."}
            if not skip and any(f["group"] == "missing" for f in fields(intake)[0]):  # e.g. read just now, and more is missing
                return 200, {**view(intake), "message": "The resume has been read. A few more details are needed."}
        made = await tools.create_profile(intake.id, fill_missing=skip)
        if not made.get("ok"):
            return 200, {**view(intake), "message": made.get("error") or "The profile couldn't be created. Please try again."}
        if made["profile"]["status"] in ("created", "duplicate"):
            await tools.match_roles(intake.id)
            await notify.form_result(intake)  # on WhatsApp the recruiter is told there too (never raises)
            return 200, view(intake)
        return 200, {**view(intake), "message": made["profile"].get("message") or "The profile couldn't be created yet."}
