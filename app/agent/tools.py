"""The tools the Perfox agent can call (MCP). They hold no logic of their own: they run the pipeline's steps
(app/pipeline.py) and read the audit log.

- process_resume: the whole pipeline in one call, in a fixed order. The default.
- provide_missing_details: when a resume lacks something Questlight requires (a job title, say) the recruiter is asked,
  and this stores the answer on the server; create_profile then creates the profile. (A recruiter who says "go ahead
  without it" gets create_profile(fill_missing=true), which saves "Not specified".)
- check_junk, parse_resume, create_profile, match_roles: the same blocks one at a time (match_roles also does the Screening
  step after a strong match, as the pipeline does), for a recruiter who asks for
  just one step. The server keeps the file and the parsed resume between calls (see intakes.py), and the rules are
  checked here whatever order the agent calls them in: nothing is parsed unless the junk check accepted the file, no
  profile is created unless the parsed resume has every required detail, and matching needs a profile.
- process_job_description: a job description (attached, or pasted as text) in, a Questlight job out, screened with the
  best candidates (app/job_pipeline.py). provide_job_details and create_job finish a JD that lacked something.
  process_resume and process_job_description each hand over to the other when a file turns out to be the other kind.
- find_candidates_for_job, add_candidates_to_job: the other direction. For an open job, rank the candidates already in
  Questlight (read-only); then, once the recruiter agrees, put chosen ones on the job at the Screening stage.
- get_intake_summary, list_recent_intakes: read the audit log. list_open_roles: Questlight's open jobs.

The text in each tool's docstring is what the agent reads. After changing one, click Rediscover on the integration in
Perfox. Add every new tool to tool_guide.py too.
"""
import asyncio
import contextlib
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app import job_pipeline, pipeline, settings
from app.agent import chat_bridge, downloads, formatting, intakes
from app.agent.mcp_app import mcp
from app.intake import candidates, corrections, jobs, matching, questlight
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
    """Process ONE candidate resume the recruiter has shared (PDF or DOCX). USE THIS BY DEFAULT for an attached file.
    If the file turns out to be a job description, the JD intake runs instead and the result has kind "job_description"
    (report it as described under process_job_description).

    Pass the URL of the uploaded file exactly as received (the attachment URL), and its file name if known.
    This runs the whole intake in a fixed order: junk check, resume parsing, creating the candidate profile in
    Questlight, ranking Questlight's open jobs for the new candidate, then (for a strong match) putting the candidate on
    the best 3 jobs at the Screening stage in Questlight. Call it once per file. If you call it
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
    - profile.resume_attached: true when the resume file is stored on the profile (Questlight also makes the masked CV
      from it). false on a created profile means it was created WITHOUT the file: say so.
    - profile.adjusted: things Questlight's rules made us fill in or shorten on the profile (e.g. "a job title was empty"
      means that job was saved with the title "Not specified"). If not empty, TELL the recruiter, in plain words.
    - info_complete and missing_required: the fields the resume lacks; missing_recommended: nice to have (phone).
    - top_roles: best matching open jobs with score 0-100 (under 40 means no strong match), matched and missing skills.
    - screening: whether the candidate was added to those jobs at the Screening stage in Questlight (status "screened",
      "partial", "failed" or "skipped", with a message). Tell the recruiter which jobs; if it failed, say so plainly.
    """
    token = _upload_token(file_url)
    earlier = intakes.find(file_url) if token else None
    if earlier and earlier.parse_done:  # already processed in this chat: say where it stands, don't run it again
        return _where_it_stands(earlier)
    return await _once(token or str(file_url or "").strip(), lambda: _resume_job(file_url, file_name, token))


async def _resume_job(file_url: str, file_name: str, token) -> dict:
    data, name, err = await downloads.download(file_url, file_name)
    if err:
        return {"ok": False, "decision": "error", "error": err, "profile_created": False}
    return await _take_resume(name, data, token)


# The intakes running right now, by file (upload token, or the https link). The whole intake takes 15-60 s and a caller may
# give up and ask again after 30 s (Perfox's default tool timeout): the second call joins the one already running instead of
# starting another, so the file isn't parsed twice at once (the shared parser struggles with overlapping requests) and a
# caller that has gone away doesn't cancel it half way.
_running: dict = {}


async def _once(key: str, start):
    task = _running.get(key)
    if task is None or task.done():
        task = asyncio.ensure_future(start())
        _running[key] = task
        task.add_done_callback(lambda t, k=key: (_running.pop(k, None) if _running.get(k) is t else None,
                                                 t.exception() if not t.cancelled() else None))
    return await asyncio.shield(task)


async def _take_resume(name: str, data: bytes, token) -> dict:
    """The resume intake for a downloaded file. A file that turns out to be a job description goes to the JD intake
    instead (pipeline.process_file does that), and the answer is the JD's."""
    state = {}
    result = await pipeline.process_file(name, data, CHANNEL, state)
    if result.get("kind"):  # it was a job description: the JD intake ran
        return _jd_answer(state, name, data, result, token)
    out = formatting.for_agent(result)
    intake = intakes.keep_for_follow_up(state, name, data, result, token)
    if intake:  # the same file can now be followed up: supply what is missing, retry, match roles
        out["file_id"] = intake.id
        hint = formatting.next_hint((out["profile"] or {}).get("status"))
        if hint:
            out["next"] = hint
    return out


def _jd_answer(state: dict, name: str, data: bytes, result: dict, token) -> dict:
    out = formatting.job_for_agent(result)
    intake = intakes.keep_jd_for_follow_up(state, name, data, result, token)
    if intake:
        out["jd_id"] = intake.id
        hint = formatting.job_next_hint(out.get("job_status"))
        if hint:
            out["next"] = hint
    return out


def _where_it_stands(intake: intakes.Intake) -> dict:
    """The answer for a file (or pasted JD) already taken in during this chat: nothing is run again."""
    if intake.kind == "jd":
        out = {"ok": True, "kind": "job_description", "file": intake.name, "decision": "job_description", "jd_id": intake.id,
               "reason": "this job description was already taken in during this chat, so nothing was run again",
               **formatting.job_view(intake.job or {"status": "not_created", "preview": job_pipeline.preview(intake.details or {})})}
        hint = formatting.job_next_hint(out.get("job_status"))
        if hint:
            out["next"] = hint
        return out
    return formatting.status_of(intake)


def _resume_intake(ref: str):
    """(intake, error) for the resume step tools: a job description's reference is refused with what to use instead."""
    intake, err = intakes.lookup(ref)
    if intake and intake.kind == "jd":
        return None, "that reference is a job description, not a resume: use provide_job_details or create_job with it"
    return intake, err


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
    intake, err = _resume_intake(file_id)
    if err or intake is None:
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
                "next": "ask the recruiter for ONLY the items in missing_items (nothing else is known to be missing), then provide_missing_details(file_id, answers): the resume is then read"}
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
    never guess a value. Returns what is still missing.

    When process_resume/parse_resume says the parser could not read the candidate's email or phone (missing_items has
    only those), nothing else of the resume has been read yet: ask for just those items. Once all are given this tool
    reads the resume itself, and the result then has the candidate and anything else still missing. If reading fails the
    result says why (ok false); calling again with answers=[] retries."""
    intake, err = _resume_intake(file_id)
    if err or intake is None:
        return {"ok": False, "error": err}
    if not intake.parse_done:
        return {"ok": False, "file_id": intake.id, "error": "this file has not been parsed yet: call parse_resume first"}
    if intake.profile and intake.profile["status"] in ("created", "duplicate"):
        return {"ok": False, "file_id": intake.id, "error": "a profile already exists for this file, so there is nothing "
                "left to complete (this tool can't edit an existing Questlight profile)"}
    unread = intake.parsed is None  # the parser stopped on missing contact details: nothing of the resume has been read
    parsed = intake.contact if unread else intake.parsed
    applied, rejected = [], []
    for answer in answers if isinstance(answers, list) else []:
        key = str(answer.get("id", "")).strip() if isinstance(answer, dict) else ""
        value = " ".join(str(answer.get("value", "")).split()) if isinstance(answer, dict) else ""
        problem = ("only the details in missing_items can be given until the resume has been read"
                   if unread and key not in ("name", "email", "phone") else corrections.apply_answer(parsed, key, value))
        (rejected.append(f"{key or '?'}: {problem}") if problem else applied.append(key))
    if applied:  # ids only (like "job_title:1"), never the values: the audit log holds no candidate details
        intake.run.log("correction", "applied", "the recruiter supplied missing details", fields=applied)
    read_error = None
    if unread:
        asked = {"phone": "phoneNumber"}
        wanted = [asked.get(i["id"], i["id"]) for i in questlight.unreadable_items(intake.incomplete)]
        if all(intake.contact.get(k) for k in wanted):  # everything the parser asked for is in: now read the resume itself
            async with _traced(intake.run, "provide_missing_details"):
                p = await pipeline.read_with_contact(intake.run, intake.name, intake.ext, intake.data, intake.text, intake.contact)
            if p["failed"]:
                read_error = p["failed"]["error"]
            else:
                intake.parsed, intake.incomplete = p["parsed"], None
        else:
            left = [i for i in questlight.unreadable_items(intake.incomplete) if not intake.contact.get(asked.get(i["id"], i["id"]))]
            return {"ok": True, "file_id": intake.id, "applied": applied, "rejected": rejected, "info_complete": False,
                    "missing_required": [formatting.UNREADABLE], "missing_items": left,
                    "next": "ask the recruiter for only the items in missing_items; once all are given the resume is read"}
    if intake.parsed is None:  # reading it again failed (the reason is in `error`): the same call retries it
        return {"ok": False, "file_id": intake.id, "applied": applied, "rejected": rejected, "error": read_error,
                "next": "tell the recruiter what went wrong; calling provide_missing_details again (with answers=[]) tries once more"}
    checked = questlight.review(intake.parsed)
    return {"ok": True, "file_id": intake.id, "applied": applied, "rejected": rejected,
            **({"candidate": formatting.candidate(pipeline.redact(intake.parsed))} if unread else {}),
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
    intake, err = _resume_intake(file_id)
    if err or intake is None:
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
    For a candidate whose profile was just created, a strong match also puts them on those 3 jobs at the Screening stage
    in Questlight (see `screening` in the result); calling this again retries only the jobs that failed.

    file_id comes from the process_resume (or check_junk) result; if you no longer have it, pass the attachment's
    file_url instead (a candidate whose resume was taken in with process_resume can be matched at any time within 30
    minutes, no profile step needed). The candidate must have been parsed and have a Questlight profile (just created by
    create_profile, or one that already existed: status "duplicate"). Refuses otherwise. THIS WRITES TO QUESTLIGHT only
    for a newly created profile (the Screening step); for an existing profile it is read-only."""
    intake, err = _resume_intake(file_id)
    if err or intake is None:
        return {"ok": False, "error": err}
    if not intake.profile or intake.profile["status"] not in ("created", "duplicate"):
        return {"ok": False, "file_id": intake.id, "error": "this candidate has no Questlight profile yet: run "
                "create_profile first (matching only runs for candidates that are in Questlight)"}
    async with _traced(intake.run, "match_roles") as trace:
        if intake.roles is None or intake.roles["status"] == "failed":
            intake.roles = await pipeline.match_open_roles(intake.run, intake.parsed, intake.text, intake.profile, allow_existing=True)
        elif (intake.roles.get("screening") or {}).get("status") in ("failed", "partial"):
            await pipeline.screen_top_roles(intake.run, intake.profile, intake.roles)  # retries only the jobs that failed
    return {"ok": True, "file_id": intake.id, "roles_status": intake.roles["status"],
            "roles_message": intake.roles["message"], "top_roles": formatting.top_roles(intake.roles),
            "screening": formatting.screening(intake.roles), "trace_id": trace.id}


async def _resolve_job(ref: str):
    """The one open job the recruiter means. Returns (job, None), or (None, the tool result to hand back instead)."""
    jobs, err = await matching.fetch_open_jobs()
    if err:
        return None, {"ok": False, "error": err}
    job, choices = matching.find_job(ref, jobs)
    if job:
        return job, None
    if choices:
        return None, {"ok": True, "needs_choice": True, "matching_jobs": len(choices),
                      "message": "more than one open job fits: ask the recruiter which one, then call again with its job_id",
                      "jobs": [_job_line(j) for j in choices[:10]]}
    return None, {"ok": False, "error": "no open job matches that (only open jobs can be used): list_open_roles(search) finds them"}


def _job_line(job: dict) -> dict:
    return {"title": str(job.get("jobPositionTitle") or "").strip(), "job_id": job.get("jobId"),
            "location": ", ".join(str(job[k]) for k in ("city", "state") if job.get(k) and not str(job[k]).isdigit())}


@mcp.tool()
async def find_candidates_for_job(job: str, top_k: int = 3) -> dict:
    """Which candidates already in Questlight fit an open job best? Read-only: nothing is written.

    job is the job ID (like JOB-020926-00005) or its title. A title that fits several open jobs returns needs_choice
    with the list: ask the recruiter which one, then call again with that job_id. top_k is how many to return (default 3,
    max 10). Candidates already hired or in onboarding are left out. Scoring is by rules (skills, experience, title,
    degree, location), not by you: report the scores as given, 0-100 (under 40 means no strong candidate), with the
    matched and missing skills. The first call takes a few seconds; later ones are quick.
    Candidates already on this job are left out (already_on_job counts them); if Questlight couldn't say who is on it,
    note explains. Do NOT add anyone to the job from here: only after the recruiter says yes to adding them, call
    add_candidates_to_job."""
    found, other = await _resolve_job(job)
    if other:
        return other
    run = audit.Run(f"job {found.get('jobId')}", b"", CHANNEL)
    async with _traced(run, "find_candidates_for_job") as trace:
        result = await candidates.find_for_job(found, top_k)
    run.log("find_candidates", result["status"], result["message"], record_id=found.get("jobId"),
            considered=result["considered"], top=[{"candidate": c["code"], "score": c["score"]} for c in result["candidates"]])
    if result["status"] == "failed":
        return {"ok": False, "job": _job_line(found), "error": result["message"], "trace_id": trace.id}
    return {"ok": True, "job": _job_line(found), "status": result["status"], "message": result["message"],
            "considered": result["considered"], "left_out_hired_or_onboarding": result["left_out"],
            "already_on_job": result.get("already_on_job", 0),
            "candidates": [{"name": c["name"], "candidate_id": c["code"], "score": c["score"], "status": c["status"],
                            "location": c["location"], "experience": c["experience"],
                            "matched_skills": c["matchedSkills"][:8], "missing_skills": c["missingSkills"][:8]}
                           for c in result["candidates"]],
            "note": result.get("on_job_check"),
            "next": "to put them on this job at the Screening stage, ask the recruiter first, then add_candidates_to_job",
            "trace_id": trace.id}


@mcp.tool()
async def add_candidates_to_job(job: str, candidate_ids: list[str]) -> dict:
    """Put candidates on an open job at the Screening stage in Questlight (status Ongoing). THIS WRITES TO QUESTLIGHT.

    ONLY call this after the recruiter has seen the candidates and clearly said to add them, and only with the IDs
    they chose. job is the job ID (or an unambiguous title); candidate_ids are candidate IDs (like CAN-061026-00003) from
    find_candidates_for_job, at most 10 per call. Anyone who is hired or in onboarding, or unknown, is refused. A
    candidate already on the job counts as done. Returns each candidate's result: "screened", "already", "failed",
    "refused" or "not_found". Tell the recruiter the outcome for each one, plainly."""
    ids = [str(c) for c in candidate_ids] if isinstance(candidate_ids, list) else []
    if not ids:
        return {"ok": False, "error": "no candidate IDs given"}
    if len(ids) > candidates.MAX_ADD:
        return {"ok": False, "error": f"at most {candidates.MAX_ADD} candidates per call"}
    found, other = await _resolve_job(job)
    if other:
        return other
    people, err = await candidates.fetch_candidates()
    if err:
        return {"ok": False, "error": err}
    chosen, problems = candidates.pick(ids, people)
    run = audit.Run(f"job {found.get('jobId')}", b"", CHANNEL)
    async with _traced(run, "add_candidates_to_job") as trace:
        with tracing.span("add candidates to screening"):
            results = problems + await candidates.add_to_job(found, chosen)
    done = [r for r in results if r["status"] in ("screened", "already")]
    status = "screened" if len(done) == len(results) else "partial" if done else "failed"
    run.log("add_to_screening", status, f"{len(done)} of {len(results)} candidates are on the job at the Screening stage",
            record_id=found.get("jobId"), candidates=[{"candidate": r["candidate"], "status": r["status"]} for r in results])
    return {"ok": True, "job": _job_line(found), "status": status, "results": results, "trace_id": trace.id}


# ---------- job descriptions: a JD in, a Questlight job out ----------

@mcp.tool()
async def process_job_description(file_url: str = "", file_name: str = "", text: str = "") -> dict:
    """Take in ONE job description (JD) and create the job in Questlight. THIS WRITES TO QUESTLIGHT.

    Pass EITHER the attached file (file_url exactly as received, and file_name if known; PDF, DOCX, TXT or DOC) OR, when
    the recruiter pasted the JD into the chat, the JD's text as `text` (copy it exactly, don't summarise). It runs in a
    fixed order: checks it is a JD, reads it with Questlight's JD parser, checks Questlight for the same job already open,
    creates the job (status Active), starts Questlight's own matching, then ranks the candidates already in Questlight and
    puts the best 3 on the new job at the Screening stage. A file that turns out to be a resume is taken in as a resume
    instead (kind "resume": report it like process_resume's result). Calling it again for the same attachment or text does
    NOT run it again: it returns where the job stands, with the jd_id.

    - job_status "created": report job_id, the job (title, client, location, experience, salary, skills), adjusted (what
      was filled in, e.g. the business head or openings: tell the recruiter), then top_candidates with scores and whether
      each was screened (screening).
    - job_status "not_created": Questlight needs details the JD lacks (missing_items, e.g. the client or the salary
      range). ASK the recruiter for each, then provide_job_details(jd_id, answers), then create_job(jd_id).
    - job_status "duplicate_found": an open job with the same title, client and city exists (duplicates). Tell the
      recruiter and ask; only if they say to create it anyway, call create_job(jd_id, allow_duplicate=true).
    - kind "junk" or "unclear": not a JD (reason says why); nothing was created."""
    text = (text or "").strip()
    if not file_url and not text:
        return {"ok": False, "error": "pass the attached file's file_url, or the pasted JD as text"}
    key = (_upload_token(file_url) or str(file_url).strip()) if file_url else intakes.pasted_token(text)
    return await _once(key, lambda: _jd_job(file_url, file_name, text))


async def _jd_job(file_url: str, file_name: str, text: str) -> dict:
    if file_url:
        token = _upload_token(file_url)
        earlier = intakes.find(file_url) if token else None
        if earlier and earlier.parse_done:
            return _where_it_stands(earlier)
        data, name, err = await downloads.download(file_url, file_name)
        if err:
            return {"ok": False, "decision": "error", "error": err, "job_created": False}
        if not token:  # an ordinary https link: the same file sent again is recognised by its content
            token = intakes.content_token(data)
            earlier = intakes.find(token)
            if earlier and earlier.parse_done:
                return _where_it_stands(earlier)
        state = {}
        result = await job_pipeline.process_jd(name, data, channel=CHANNEL, state=state)
        if result.get("kind") == "resume":  # a resume after all: the resume intake takes it
            out = await _take_resume(name, data, token)
            out["kind"] = "resume"
            return out
        return _jd_answer(state, name, data, result, token)
    token = intakes.pasted_token(text)
    earlier = intakes.find(token)
    if earlier and earlier.kind == "jd":
        return _where_it_stands(earlier)
    state = {}
    result = await job_pipeline.process_jd(job_pipeline.PASTED, None, text, CHANNEL, state)
    if result.get("kind") == "resume":
        return {"ok": False, "kind": "resume", "error": "this text looks like a resume, not a job description: a resume has to be "
                "attached as a PDF or DOCX file"}
    return _jd_answer(state, job_pipeline.PASTED, text.encode("utf-8"), result, token)




def _jd_intake(ref: str):
    intake, err = intakes.lookup(ref)
    if intake and intake.kind != "jd":
        return None, "that reference is a resume, not a job description: use the resume tools with it"
    return intake, err


@mcp.tool()
async def provide_job_details(jd_id: str, answers: list[dict[str, str]]) -> dict:
    """Store details the recruiter supplied for what a job description lacked. Nothing is written to Questlight by this
    tool: call create_job(jd_id) afterwards.

    jd_id comes from the process_job_description result (if you no longer have it, pass the attachment's file_url). answers
    is a list of {"id": ..., "value": ...} using the ids from missing_items: title, summary, client (a client name),
    business_head (a person's name), location ("City" or "City, State"), experience ("4-8"), salary ("12-18 LPA"),
    skills and skill_domains (comma-separated), industry. Use ONLY values the recruiter actually said: never guess. A
    client or business head that doesn't match exactly one in Questlight is rejected with the list to choose from: show
    it to the recruiter. Returns what is still missing."""
    intake, err = _jd_intake(jd_id)
    if err or intake is None:
        return {"ok": False, "error": err}
    if (intake.job or {}).get("status") == "created":
        return {"ok": False, "jd_id": intake.id, "error": "the job was already created in Questlight; it can't be edited from here"}
    details = intake.details or {}
    applied, rejected = [], []
    for answer in answers if isinstance(answers, list) else []:
        key = str(answer.get("id", "")).strip() if isinstance(answer, dict) else ""
        value = str(answer.get("value", "")) if isinstance(answer, dict) else ""
        problem = await jobs.apply_answer(details, key, value)
        (rejected.append(f"{key or '?'}: {problem}") if problem else applied.append(key))
    intake.details = details
    if applied:  # ids only, never the values
        intake.run.log("job_details", "applied", "the recruiter supplied missing job details", fields=applied)
    checked = jobs.review(details)
    return {"ok": True, "jd_id": intake.id, "applied": applied, "rejected": rejected, "info_complete": not checked["missing"],
            "missing_items": checked["items"], "job": job_pipeline.preview(details),
            "next": "create_job(jd_id)" if not checked["missing"] else "ask the recruiter for what is still missing, then call this again"}


@mcp.tool()
async def create_job(jd_id: str, allow_duplicate: bool = False) -> dict:
    """Create the job from a job description that was taken in, then screen the best candidates for it, exactly as
    process_job_description does. THIS WRITES TO QUESTLIGHT.

    Use it after provide_job_details has filled what was missing, or with allow_duplicate=true ONLY when the recruiter
    said to create the job although a similar one is open. jd_id comes from process_job_description (or pass the
    attachment's file_url). Refuses (nothing is written) while required details are missing. Safe to repeat: a job that
    was created is never created twice; the saved result is returned."""
    intake, err = _jd_intake(jd_id)
    if err or intake is None:
        return {"ok": False, "error": err}
    if (intake.job or {}).get("status") != "created":
        async with _traced(intake.run, "create_job") as trace:
            intake.job = await job_pipeline.create_and_scan(intake.run, intake.details or {}, allow_duplicate=allow_duplicate)
        trace_id = trace.id
    else:
        trace_id = None
    out = {"ok": True, "kind": "job_description", "jd_id": intake.id, **formatting.job_view(intake.job or {}), "trace_id": trace_id}
    hint = formatting.job_next_hint(out.get("job_status"))
    if hint:
        out["next"] = hint
    return out


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
    matching: ok (a strong match found) / no_strong_match / failed. job_screening: screened / partial / failed / skipped
    (candidates put on their top jobs at the Screening stage). added_to_jobs: how often recruiters added candidates to a
    job themselves (screened / partial / failed). job_descriptions: JDs taken in (by decision). jobs: created /
    not_created (details missing) / duplicate_found / failed. jd_screening: candidates screened for new jobs. errors: files that could not be read or parsed.
    """
    days = max(1, min(int(days), 90))
    since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat(timespec="seconds")
    counts = audit.summary(since)
    return {"period": f"last {days} day(s)", "since_utc": since, "files_received": audit.run_count(since),
            "screening": counts.get("junk_check", {}), "profiles": counts.get("load_questlight", {}),
            "matching": counts.get("match_roles", {}), "job_screening": counts.get("screen_top_roles", {}),
            "added_to_jobs": counts.get("add_to_screening", {}),
            "job_descriptions": counts.get("jd_check", {}), "jobs": counts.get("create_job", {}),
            "jd_screening": counts.get("screen_candidates", {}),
            "errors": {step: n["error"] for step, n in counts.items() if "error" in n}}


@mcp.tool()
async def list_recent_intakes(limit: int = 10) -> dict:
    """The most recent resumes received (newest first, max 50): when, file name, channel, the screening decision,
    the profile outcome, the Questlight candidate ID if one was created, and the last step's note."""
    runs = audit.recent_runs(max(1, min(int(limit), 50)))
    return {"intakes": [{"received_utc": r["ts"], "file": r["file"], "channel": r["channel"],
                         "screening": r["steps"].get("junk_check", r["steps"].get("intake")),
                         "profile": r["steps"].get("load_questlight"), "matching": r["steps"].get("match_roles"),
                         "job_screening": r["steps"].get("screen_top_roles"),
                         "job": r["steps"].get("create_job"),
                         "candidate_id": r["record_id"], "note": r["last_reason"]} for r in runs]}
