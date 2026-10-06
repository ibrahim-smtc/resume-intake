"""The intake pipeline: one resume in, one decision out. The BRD workflow blocks, in order:

    intake checks -> "Junk?" -> parse -> "Load into Questlight" -> "Match open roles" -> screen the top 3

The blocks themselves are in app/intake (junk rules, the two parsers, the Questlight profile call, the job matching);
this module runs them in order, writes the audit log and the trace as it goes, and shapes the result. The upload page, the
Perfox agent's tools (app/agent) and the tests all call process_file() or the individual steps below.

AI is used only to read the resume (the parser). Every other decision is plain code, including which jobs the
candidate is put on for screening: the best 3 matches, no more.
"""
from pathlib import Path

import httpx

from app import settings
from app.intake import documents, junk, matching, parser_perfox, parser_questlight, questlight
from app.observability import audit, tracing

ALLOWED_TYPES = (".pdf", ".docx")

# Fields in the parsed JSON that identify or contact the candidate. Questlight's own masked CV removes email, phone and
# LinkedIn but keeps name and location; this mirrors it.
REDACT_FIELDS = ["email", "phoneNumber", "linkedInProfile", "github", "portfolio", "dateOfBirth"]
REDACTED = "[REDACTED]"
NOT_CREATED = "required information is missing, so no profile was created"


def redact(parsed: dict) -> dict:
    if not settings.MASKING:  # masking is off: the page and the agent see the parsed data as it is
        return parsed
    return {k: (REDACTED if k in REDACT_FIELDS and v else v) for k, v in parsed.items()}


def fail(run: audit.Run, step: str, error: str) -> dict:
    run.log(step, "error", error)
    tracing.current().fail(error)
    return {"file": run.file, "ok": False, "error": error}


def stopped(name: str, verdict) -> dict:
    """The junk check ended the run (junk, or sent to the review queue): nothing is parsed."""
    return {"file": name, "ok": True, "decision": verdict.decision, "reason": verdict.reason, "redacted": None,
            "missing_fields": [], "missing_recommended": [], "profile": None, "roles": None,
            "masked_pdf_b64": None, "warning": None}


def success(name: str, verdict, parsed, masked_b64, warning, missing, recommended, profile, roles=None) -> dict:
    # Nothing is written to disk, and only the redacted JSON is returned.
    return {"file": name, "ok": True, "decision": verdict.decision, "reason": verdict.reason, "masking": settings.MASKING,
            "redacted": redact(parsed) if parsed else None, "missing_fields": missing,
            "missing_recommended": recommended, "profile": profile, "roles": roles,
            "masked_pdf_b64": masked_b64, "warning": warning}


# ---------- the steps ----------

def intake_error(ext: str, data: bytes):
    if ext not in ALLOWED_TYPES:
        return f"unsupported file type ({ext or 'no extension'}): the parser takes PDF or DOCX"
    if not data:
        return "empty file"
    if len(data) > settings.MAX_UPLOAD_BYTES:
        return f"file is larger than {settings.MAX_UPLOAD_LABEL}"
    return None


def junk_step(run: audit.Run, name: str, ext: str, data: bytes):
    """The intake checks and the "Junk?" block, decided locally from the file's own text before any parser call.
    Returns (text, verdict, failure): failure is the finished error result when the file can't be read, else None."""
    with tracing.span("intake checks"):
        error = intake_error(ext, data)
        if error:
            return None, None, fail(run, "intake", error)
    with tracing.span("junk check") as s:
        text, has_images, read_err = documents.extract_text(data, ext)
        if read_err:
            return None, None, fail(run, "junk_check", read_err)
        verdict = junk.classify(text, has_images)
        s.set(decision=verdict.decision, words=verdict.signals["words"])
    run.log("junk_check", verdict.decision, verdict.reason, **verdict.signals)
    print(f"[intake] {name}: {verdict.decision} - {verdict.reason}")
    return text, verdict, None


async def parse_step(run: audit.Run, name: str, ext: str, data: bytes) -> dict:
    """The parse block, with whichever parser settings.PARSER selects. Returns a dict with
    parsed (the resume JSON, or None), failed (the finished error result, or None),
    incomplete (Questlight's "name/email/phone missing" message: a real resume, but there is no JSON), and
    masked_b64 / warning (only the Questlight parser can mask)."""
    out = {"parsed": None, "failed": None, "incomplete": None, "masked_b64": None, "warning": None}
    if settings.PARSER == "perfox":
        with tracing.span("parse resume", kind="llm") as s:
            s.set_usage(provider="perfox")
            parsed, err = await parser_perfox.parse_resume(name, data, ext)
            if err:
                out["failed"] = fail(run, "parse", err)
                return out
        run.log("parse", "ok", "parsed with the temporary Perfox parser", parser=settings.PARSER)
        out["parsed"] = parsed
        out["warning"] = "no masked PDF while the temporary Perfox parser is on (only Questlight's service can mask)"
        return out

    async with httpx.AsyncClient(timeout=httpx.Timeout(settings.PARSER_TIMEOUT_S)) as client:
        with tracing.span("parse resume", kind="llm") as s:
            s.set_usage(provider="questlight")
            parsed, err = await parser_questlight.parse(client, name, data, ext)
            if err and err.startswith(parser_questlight.MISSING_MESSAGE):
                # A real resume that lacks name/email/phone: Questlight returns no JSON, so there is nothing to load.
                s.set(outcome="required fields missing")
                run.log("parse", "incomplete", err, parser=settings.PARSER)
                out["incomplete"] = out["warning"] = err
                return out
            if err:
                out["failed"] = fail(run, "parse", err)
                return out
        run.log("parse", "ok", "parsed with Questlight's parser", parser=settings.PARSER)
        out["parsed"] = parsed
        if settings.MASKING:  # off by default: the mask call is skipped entirely, so it adds no time and no failure point
            with tracing.span("mask PDF") as s:
                out["masked_b64"], out["warning"] = await parser_questlight.mask(client, name, data, ext)
                if out["warning"]:
                    s.set(note=out["warning"])
                    if ext in parser_questlight.MASKABLE:
                        s.fail(out["warning"])
    return out


async def load_into_questlight(run: audit.Run, data: bytes, ext: str, parsed: dict, fill: bool = False):
    """The "Load into Questlight" block. Returns (missing required fields, missing recommended fields, profile result).
    The profile is created from the real parsed data; only the redacted copy ever goes back to the page.
    Details Questlight requires but the resume lacks (a job title, say) block the profile and are listed in `missing`
    (and as profile["missing_items"]) so they can be asked for; fill=True saves them as "Not specified" instead."""
    with tracing.span("load into Questlight") as s:
        checked = questlight.review(parsed, fill=fill, source=run.channel)
        applicant, missing, adjusted = checked["applicant"], checked["missing"], checked["adjusted"]
        recommended = questlight.missing_recommended(applicant)
        if missing:
            profile = {"status": "not_created", "message": NOT_CREATED, "missing_items": checked["items"]}
        else:
            profile = await questlight.create_profile(applicant, run.file, data, ext)
        if adjusted:  # what Questlight's rules made us fill in or shorten, so the recruiter is told
            profile["adjusted"] = adjusted
        kinds = [m.split(" for ", 1)[0] for m in missing]  # the audit log and trace never hold candidate details, so no company names
        s.set(outcome=profile["status"], missing_required=kinds, filled_in=fill)
        if profile["status"] == "failed":
            s.fail(profile["message"])
    run.log("load_questlight", profile["status"], profile["message"], record_id=profile.get("applicantId"),
            missing_required=kinds, missing_recommended=recommended)
    return missing, recommended, profile


async def match_open_roles(run: audit.Run, parsed: dict, text: str, profile: dict, allow_existing: bool = False):
    """The "Match open roles" block. In the pipeline it runs only for a candidate whose profile was just created in
    Questlight; allow_existing=True (the agent's match_roles tool, when a recruiter asks) also lets a candidate
    whose profile already existed (a duplicate) through. A strong match ends with screen_top_roles (a new profile only)."""
    if profile["status"] != "created" and not (allow_existing and profile["status"] == "duplicate"):
        return None
    with tracing.span("match open roles") as s:
        result = await matching.match_roles(parsed, text)
        s.set(outcome=result["status"], open_jobs=result["open_jobs"])
        if result["status"] == "failed":
            s.fail(result["message"])
    run.log("match_roles", result["status"], result["message"], record_id=profile.get("applicantId"),
            open_jobs=result["open_jobs"], top=[{"jobId": m["jobId"], "score": m["score"]} for m in result["matches"]])
    await screen_top_roles(run, profile, result)
    return result


async def screen_top_roles(run: audit.Run, profile: dict, roles: dict):
    """After matching: puts a candidate whose profile was just created on their best matching jobs (up to 3) at the Screening
    stage in Questlight. The outcome is stored on roles["screening"] and on each match as m["screening"]. Runs only for a
    new profile (an existing one has no ID here) and only when matching found a strong match. Safe to call again: jobs
    already done are skipped and only the ones that failed are retried. Returns roles["screening"], or None when the
    candidate is not a new profile or there are no matches."""
    if profile.get("status") != "created" or not roles or not roles.get("matches"):
        return None
    with tracing.span("screen top roles") as s:
        if roles["status"] != "ok":
            roles["screening"] = {"status": "skipped", "message": "no strong match, so the candidate was not put on any job"}
        elif not profile.get("id"):
            roles["screening"] = {"status": "skipped", "message": "Questlight returned no ID for the new profile, so it can't be put on a job"}
        else:
            for m in roles["matches"][:matching.TOP_N]:
                if (m.get("screening") or {}).get("status") not in ("screened", "already"):
                    m["screening"] = await questlight.add_to_screening(profile["id"], m["id"])
            done = [m for m in roles["matches"][:matching.TOP_N] if m["screening"]["status"] in ("screened", "already")]
            titles = ", ".join(m["title"] for m in done)
            first_error = next((m["screening"]["message"] for m in roles["matches"][:matching.TOP_N] if m["screening"]["status"] == "failed"), None)
            if len(done) == len(roles["matches"][:matching.TOP_N]):
                roles["screening"] = {"status": "screened", "message": f"added to the Screening stage of {len(done)} job(s): {titles}"}
            elif done:
                roles["screening"] = {"status": "partial", "message": f"added to the Screening stage of {titles}; the rest failed: {first_error}"}
            else:
                roles["screening"] = {"status": "failed", "message": f"could not add the candidate to any job: {first_error}"}
        outcome = roles["screening"]
        s.set(outcome=outcome["status"])
        if outcome["status"] in ("failed", "partial"):
            s.fail(outcome["message"])
    run.log("screen_top_roles", outcome["status"], outcome["message"], record_id=profile.get("applicantId"),
            jobs=[{"jobId": m["jobId"], "status": (m.get("screening") or {}).get("status")} for m in roles["matches"][:matching.TOP_N]])
    return outcome


# ---------- the whole run ----------

async def run_pipeline(run: audit.Run, name: str, ext: str, data: bytes, state: dict = None) -> dict:
    text, verdict, failure = junk_step(run, name, ext, data)
    if failure:
        return failure
    if verdict.decision != "accepted":
        return stopped(name, verdict)

    p = await parse_step(run, name, ext, data)
    if p["failed"]:
        return p["failed"]
    if state is not None:
        state.update(text=text, parsed=p["parsed"], incomplete=p["incomplete"])
    if p["incomplete"]:
        profile = {"status": "not_created", "message": NOT_CREATED, "missing_items": questlight.unreadable_items(p["incomplete"])}
        run.log("load_questlight", profile["status"], profile["message"])
        return success(name, verdict, None, None, p["incomplete"], ["see note"], [], profile)
    missing, recommended, profile = await load_into_questlight(run, data, ext, p["parsed"])
    roles = await match_open_roles(run, p["parsed"], text, profile)
    return success(name, verdict, p["parsed"], p["masked_b64"], p["warning"], missing, recommended, profile, roles)


async def process_file(name: str, data: bytes, channel: str = audit.CHANNEL, state: dict = None) -> dict:
    """Runs one file through the whole pipeline. Used by the upload page and by the Perfox agent's MCP tool.
    One file = one trace: every step runs in a span that records its time and, for LLM calls, tokens.
    state (optional): a dict this fills with the run, the resume text and the parsed JSON, so the agent's tools can go
    on with the same file (supplying a missing detail, then creating the profile). It never leaves the server."""
    ext = Path(name).suffix.lower()
    run = audit.Run(name, data, channel)
    if state is not None:
        state["run"] = run
    trace = tracing.Trace(run.run_id)
    try:
        with trace.span("resume intake", kind="chain") as root:
            root.set(parser=settings.PARSER, channel=channel, file_type=ext or "none", size_kb=round(len(data) / 1024, 1))
            result = await run_pipeline(run, name, ext, data, state)
            if not result["ok"]:
                root.fail(result["error"])
    finally:
        trace.save()
    result["parser"] = settings.PARSER
    result["trace"] = trace.summary()
    return result
