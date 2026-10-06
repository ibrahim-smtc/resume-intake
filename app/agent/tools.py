"""The tools the Perfox agent can call (MCP). They hold no logic of their own: they run the pipeline's steps
(app/pipeline.py) and read the audit log.

- process_resume: the whole pipeline in one call, in a fixed order. The default.
- provide_missing_details: when a resume lacks something Questlight requires (a job title, say) the recruiter is asked,
  and this stores the answer on the server; create_profile then creates the profile. (A recruiter who says "go ahead
  without it" gets create_profile(fill_missing=true), which saves "Not specified".)
- check_junk, parse_resume, create_profile, match_roles: the same blocks one at a time, for a recruiter who asks for
  just one step. The server keeps the file and the parsed resume between calls (see intakes.py), and the rules are
  checked here whatever order the agent calls them in: nothing is parsed unless the junk check accepted the file, no
  profile is created unless the parsed resume has every required detail, and matching needs a profile.
- get_intake_summary, list_recent_intakes: read the audit log. list_open_roles: Questlight's open jobs.

The text in each tool's docstring is what the agent reads. After changing one, click Rediscover on the integration in
Perfox. Add every new tool to tool_guide.py too.
"""
import contextlib
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app import pipeline, settings
from app.agent import chat_bridge, downloads, formatting, intakes
from app.agent.mcp_app import mcp
from app.intake import corrections, matching, questlight
from app.observability import audit, tracing

CHANNEL = "perfox agent"  # the audit log's channel column for files that came in through the agent


@contextlib.asynccontextmanager
async def _traced(run: audit.Run, tool: str):
    """One agent tool call = one trace, like one upload on the page. Its id is the run id plus the tool name."""
    trace = tracing.Trace(f"{run.run_id}-{tool}")
    try:
        with trace.span(tool, kind="chain") as root:
            root.set(parser=settings.PARSER, channel=run.channel, tool=tool)
            yield trace
    finally:
        trace.save()


def _upload_token(file_url: str):
    return chat_bridge.token_of(file_url) if chat_bridge.is_link(file_url) else None


@mcp.tool()
async def process_resume(file_url: str, file_name: str = "") -> dict:
    """Process ONE candidate resume the recruiter has shared (PDF or DOCX). USE THIS BY DEFAULT.

    Pass the URL of the uploaded file exactly as received (the attachment URL), and its file name if known.
    This runs the whole intake in a fixed order: junk check, resume parsing, creating the candidate profile in
    Questlight, then ranking Questlight's open jobs for the new candidate. Call it once per file. If you call it
    again for the same attachment, it does NOT run again: it returns where that file stands now (so it is safe, and a
    way to get the file_id back). Only use the step tools (check_junk,
    parse_resume, create_profile, match_roles) when the recruiter asks for one specific step.

    If the resume lacks something Questlight requires (profile.status "not_created" with missing_items, e.g. "a job
    title for the job at Acme"), no profile is created yet and the result has a file_id: ASK the recruiter for each
    missing item (one answer that clearly covers several items may be reused), then call provide_missing_details with
    the answers, then create_profile(file_id). If the recruiter says to go ahead without it, call
    create_profile(file_id, fill_missing=true).

    Result fields to report to the recruiter:
    - decision: "accepted" (a resume), "junk" (not a resume, discarded), "needs_review" (unclear, a person must look),
      or "error" (see error).
    - candidate: name, contact details, experience, skills, as read from the resume.
    - profile_created / profile.status: "created" (new profile, see candidate_id), "duplicate" (a profile with this
      email already exists, nothing changed), "not_created" (required info missing), "failed" or "skipped".
    - profile.adjusted: things Questlight's rules made us fill in or shorten on the profile (e.g. "a job title was empty"
      means that job was saved with the title "Not specified"). If not empty, TELL the recruiter, in plain words.
    - info_complete and missing_required: the fields the resume lacks; missing_recommended: nice to have (phone).
    - top_roles: best matching open jobs with score 0-100 (under 40 means no strong match), matched and missing skills.
    """
    token = _upload_token(file_url)
    earlier = intakes.find(file_url) if token else None
    if earlier and earlier.parse_done:  # already processed in this chat: say where it stands, don't run it again
        return formatting.status_of(earlier)
    data, name, err = await downloads.download(file_url, file_name)
    if err:
        return {"ok": False, "decision": "error", "error": err, "profile_created": False}
    state = {}
    result = await pipeline.process_file(name, data, CHANNEL, state)
    out = formatting.for_agent(result)
    intake = intakes.keep_for_follow_up(state, name, data, result, token)
    if intake:  # the same file can now be followed up: supply what is missing, retry, match roles
        out["file_id"] = intake.id
        hint = formatting.next_hint((out["profile"] or {}).get("status"))
        if hint:
            out["next"] = hint
    return out


@mcp.tool()
async def check_junk(file_url: str, file_name: str = "") -> dict:
    """STEP 1 of 4 (only when the recruiter wants just the screening): check whether a file is a resume.

    Reads the file locally (no AI) and decides: "accepted" (looks like a resume), "junk" (not a resume),
    "needs_review" (unclear, a person must look) or "error" (unreadable, wrong type, too large).
    When accepted, the result includes a file_id that parse_resume, create_profile and match_roles need.
    For a normal "take in this resume" request use process_resume instead."""
    data, name, err = await downloads.download(file_url, file_name)
    if err:
        return {"ok": False, "decision": "error", "error": err}
    ext = Path(name).suffix.lower()
    run = audit.Run(name, data, CHANNEL)
    async with _traced(run, "check_junk") as trace:
        text, verdict, failure = pipeline.junk_step(run, name, ext, data)
    if failure:
        return {"ok": False, "decision": "error", "file": name, "error": failure["error"], "trace_id": trace.id}
    out = {"ok": True, "file": name, "decision": verdict.decision, "reason": verdict.reason, "trace_id": trace.id}
    if verdict.decision == "accepted":
        fresh = intakes.Intake(run, name, ext, data, text)
        fresh.upload = _upload_token(file_url)
        intakes.remember(fresh)
        out["file_id"] = run.run_id
        out["next"] = "parse_resume(file_id) reads the resume; create_profile and match_roles come after it"
    else:
        out["next"] = "nothing more to do with this file: it was not accepted as a resume"
    return out


@mcp.tool()
async def parse_resume(file_id: str) -> dict:
    """STEP 2 of 4: read an accepted resume into structured data (name, contact, experience, skills, education).

    file_id comes from check_junk (or the process_resume result); if you no longer have it, pass the attachment's
    file_url instead. Uses the configured resume parser (an AI call, about 6-15 seconds). Reports
    info_complete and the missing fields. Nothing is written to Questlight yet. Safe to repeat: the second call
    returns the saved result without parsing again."""
    intake, err = intakes.lookup(file_id)
    if err:
        return {"ok": False, "error": err}
    async with _traced(intake.run, "parse_resume") as trace:
        if not intake.parse_done:
            p = await pipeline.parse_step(intake.run, intake.name, intake.ext, intake.data)
            if p["failed"]:
                return {"ok": False, "file_id": intake.id, "error": p["failed"]["error"], "trace_id": trace.id}
            intake.parse_done, intake.parsed, intake.incomplete = True, p["parsed"], p["incomplete"]
    if intake.parsed is None:
        return {"ok": True, "file_id": intake.id, "parser": settings.PARSER, "info_complete": False, "candidate": None,
                "missing_required": [formatting.UNREADABLE], "missing_items": questlight.unreadable_items(intake.incomplete),
                "missing_recommended": [], "note": intake.incomplete, "trace_id": trace.id,
                "next": "ask the recruiter for the candidate's name and email, then provide_missing_details(file_id, answers)"}
    checked = questlight.review(intake.parsed)
    return {"ok": True, "file_id": intake.id, "parser": settings.PARSER, "candidate": formatting.candidate(pipeline.redact(intake.parsed)),
            "info_complete": not checked["missing"], "missing_required": checked["missing"], "missing_items": checked["items"],
            "missing_recommended": questlight.missing_recommended(checked["applicant"]), "trace_id": trace.id,
            "next": "create_profile(file_id)" if not checked["missing"] else
                    "ask the recruiter for each missing item, then provide_missing_details(file_id, answers)"}


@mcp.tool()
async def provide_missing_details(file_id: str, answers: list[dict[str, str]]) -> dict:
    """Store details the recruiter supplied for what the resume lacked. Nothing is written to Questlight by this tool:
    call create_profile(file_id) afterwards.

    file_id comes from the process_resume (or check_junk) result; if you no longer have it, pass the attachment's
    file_url instead. answers is a list of {"id": ..., "value": ...}, using the ids from
    missing_items, for example {"id": "job_title:1", "value": "Data Engineer"}. The ids are job_title:N, job_company:N,
    degree:N, institution:N (N = the entry's number as given in missing_items), and name, email, phone, address or
    skills (a comma-separated list). Use ONLY ids from missing_items and only values the recruiter actually said:
    never guess a value. Returns what is still missing."""
    intake, err = intakes.lookup(file_id)
    if err:
        return {"ok": False, "error": err}
    if not intake.parse_done:
        return {"ok": False, "file_id": intake.id, "error": "this file has not been parsed yet: call parse_resume first"}
    if intake.profile and intake.profile["status"] in ("created", "duplicate"):
        return {"ok": False, "file_id": intake.id, "error": "a profile already exists for this file, so there is nothing "
                "left to complete (this tool can't edit an existing Questlight profile)"}
    parsed = intake.parsed if intake.parsed is not None else {}
    applied, rejected = [], []
    for answer in answers if isinstance(answers, list) else []:
        key = str(answer.get("id", "")).strip() if isinstance(answer, dict) else ""
        value = " ".join(str(answer.get("value", "")).split()) if isinstance(answer, dict) else ""
        problem = corrections.apply_answer(parsed, key, value)
        (rejected.append(f"{key or '?'}: {problem}") if problem else applied.append(key))
    intake.parsed = parsed
    if applied:  # ids only (like "job_title:1"), never the values: the audit log holds no candidate details
        intake.run.log("correction", "applied", "the recruiter supplied missing details", fields=applied)
    checked = questlight.review(parsed)
    return {"ok": True, "file_id": intake.id, "applied": applied, "rejected": rejected,
            "info_complete": not checked["missing"], "missing_required": checked["missing"], "missing_items": checked["items"],
            "next": "create_profile(file_id)" if not checked["missing"] else
                    "ask the recruiter for what is still missing, then call this again"}


@mcp.tool()
async def create_profile(file_id: str, fill_missing: bool = False) -> dict:
    """STEP 3 of 4: create the candidate's profile in Questlight from the parsed resume. THIS WRITES TO QUESTLIGHT.

    file_id comes from the process_resume (or check_junk) result; if you no longer have it, pass the attachment's
    file_url instead (the same file). The resume must
    have been parsed. Refuses (nothing is written) when a required detail is missing: ask the recruiter for it and
    store the answer with provide_missing_details first. Only when the recruiter says to go ahead without it, pass
    fill_missing=true: the missing job titles, company names, degrees or institutions are saved as "Not specified".
    status: "created" (see candidate_id), "duplicate" (a profile with this email exists, nothing changed),
    "not_created" (required info missing, see missing_items), "failed" or "skipped". Safe to repeat: a created or
    duplicate result is returned as saved, never sent to Questlight twice; anything else is tried again."""
    intake, err = intakes.lookup(file_id)
    if err:
        return {"ok": False, "error": err}
    if not intake.parse_done:
        return {"ok": False, "file_id": intake.id, "error": "this file has not been parsed yet: call parse_resume first"}
    async with _traced(intake.run, "create_profile") as trace:
        if intake.parsed is None:  # the parser could not read name, email or phone, and nothing has been supplied
            profile = {"status": "not_created", "message": pipeline.NOT_CREATED,
                       "missing_items": questlight.unreadable_items(intake.incomplete)}
            missing, recommended = [formatting.UNREADABLE], []
        elif intake.profile and intake.profile["status"] in ("created", "duplicate"):
            profile = intake.profile
            checked = questlight.review(intake.parsed)
            missing, recommended = [], questlight.missing_recommended(checked["applicant"])
        else:
            missing, recommended, profile = await pipeline.load_into_questlight(
                intake.run, intake.data, intake.ext, intake.parsed, fill=fill_missing)
            intake.profile = profile
    return {"ok": True, "file_id": intake.id, "profile_created": profile["status"] == "created",
            "profile": formatting.profile_view(profile),
            "info_complete": not missing, "missing_required": missing, "missing_items": profile.get("missing_items") or [],
            "missing_recommended": recommended, "trace_id": trace.id}


@mcp.tool()
async def match_roles(file_id: str) -> dict:
    """STEP 4 of 4: rank Questlight's open jobs for a candidate (best 3, score 0-100, under 40 = no strong match).

    file_id comes from the process_resume (or check_junk) result; if you no longer have it, pass the attachment's
    file_url instead (a candidate whose resume was taken in with process_resume can be matched at any time within 30
    minutes, no profile step needed). The candidate must have been parsed and have a Questlight profile (just created by
    create_profile, or one that already existed: status "duplicate"). Refuses otherwise. Read-only."""
    intake, err = intakes.lookup(file_id)
    if err:
        return {"ok": False, "error": err}
    if not intake.profile or intake.profile["status"] not in ("created", "duplicate"):
        return {"ok": False, "file_id": intake.id, "error": "this candidate has no Questlight profile yet: run "
                "create_profile first (matching only runs for candidates that are in Questlight)"}
    async with _traced(intake.run, "match_roles") as trace:
        if intake.roles is None or intake.roles["status"] == "failed":
            intake.roles = await pipeline.match_open_roles(intake.run, intake.parsed, intake.text, intake.profile, allow_existing=True)
    return {"ok": True, "file_id": intake.id, "roles_status": intake.roles["status"],
            "roles_message": intake.roles["message"], "top_roles": formatting.top_roles(intake.roles), "trace_id": trace.id}


@mcp.tool()
async def list_open_roles(search: str = "", limit: int = 10) -> dict:
    """How many job roles are open in Questlight right now, and a sample of them (title, job id, location).

    search (optional) keeps only roles whose title contains that text, e.g. "python". limit is how many to list (max 30).
    open_roles is the total count (of all open roles, or of those matching search). Read-only; the list is refreshed
    from Questlight every 10 minutes. Questlight's dev data contains some test jobs."""
    jobs, err = await matching.fetch_open_jobs()
    if err:
        return {"ok": False, "error": err}
    wanted = search.strip().lower()
    chosen = [j for j in jobs if not wanted or wanted in str(j.get("jobPositionTitle") or "").lower()]
    limit = max(1, min(int(limit), 30))
    return {"ok": True, "open_roles": len(chosen), "all_open_roles": len(jobs), "search": search.strip() or None,
            "shown": [{"title": str(j.get("jobPositionTitle") or "").strip(), "job_id": j.get("jobId"),
                       "location": ", ".join(str(j[k]) for k in ("city", "state") if j.get(k) and not str(j[k]).isdigit())}
                      for j in chosen[:limit]]}


@mcp.tool()
async def get_intake_summary(days: int = 1) -> dict:
    """Counts of resumes received over the last N days (1 = last 24 hours, 7 = last week, max 90), from the audit log.

    screening: accepted / junk / needs_review. profiles: created / duplicate / not_created (info missing) / failed.
    matching: ok (a strong match found) / no_strong_match / failed. errors: files that could not be read or parsed.
    """
    days = max(1, min(int(days), 90))
    since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat(timespec="seconds")
    counts = audit.summary(since)
    return {"period": f"last {days} day(s)", "since_utc": since, "files_received": audit.run_count(since),
            "screening": counts.get("junk_check", {}), "profiles": counts.get("load_questlight", {}),
            "matching": counts.get("match_roles", {}),
            "errors": {step: n["error"] for step, n in counts.items() if "error" in n}}


@mcp.tool()
async def list_recent_intakes(limit: int = 10) -> dict:
    """The most recent resumes received (newest first, max 50): when, file name, channel, the screening decision,
    the profile outcome, the Questlight candidate ID if one was created, and the last step's note."""
    runs = audit.recent_runs(max(1, min(int(limit), 50)))
    return {"intakes": [{"received_utc": r["ts"], "file": r["file"], "channel": r["channel"],
                         "screening": r["steps"].get("junk_check", r["steps"].get("intake")),
                         "profile": r["steps"].get("load_questlight"), "matching": r["steps"].get("match_roles"),
                         "candidate_id": r["record_id"], "note": r["last_reason"]} for r in runs]}
