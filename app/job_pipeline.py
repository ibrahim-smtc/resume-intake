"""The JD intake: one job description in, one Questlight job out, screened with the best candidates already in Questlight.

    intake checks -> "What is it?" -> parse the JD -> check duplicates -> create the job
                  -> start Questlight's own matching -> rank the candidates -> screen the top 3

The blocks live in app/intake: junk.py tells a JD from a resume, jobs.py reads the JD and builds and creates the job,
candidates.py ranks the candidates already in Questlight. This module runs them in order, writes the audit log and the trace,
and shapes the result. The agent's process_job_description and create_job tools call it, and so does the upload page.

AI is used only to read the JD (Questlight's JD parser). Questlight's own create call also uses AI inside it, to write the
job's summary and screening questions; that is Questlight's doing, not ours.
"""
from pathlib import Path

from app import settings
from app.intake import candidates, documents, junk, jobs, matching, questlight
from app.observability import audit, tracing

JD_TYPES = (".pdf", ".docx", ".txt", ".doc")
PASTED = "pasted text"
MAX_PASTE = 30000  # characters


def _fail(run: audit.Run, step: str, error: str) -> dict:
    run.log(step, "error", error)
    tracing.current().fail(error)
    return {"ok": False, "kind": "job_description", "file": run.file, "decision": "error", "error": error}


def preview(details: dict) -> dict:
    """The job as it stands, in plain words, for the recruiter (no ids)."""
    d = details or {}
    rng = lambda a, b, unit: (f"{a:g}-{b:g} {unit}" if a is not None and b is not None and a != b  # noqa: E731
                              else f"{(a if a is not None else b):g} {unit}" if (a is not None or b is not None) else None)
    lakh = lambda x: x / 100000 if x else None  # noqa: E731
    return {"title": d.get("title") or None, "client": d.get("client_name") or None,
            "location": ", ".join(x for x in (d.get("city"), d.get("state"), d.get("country")) if x) or None,
            "experience": rng(d.get("min_exp"), d.get("max_exp"), "years"),
            "salary": rng(lakh(d.get("salary_min")), lakh(d.get("salary_max")), "LPA"),
            "skills": (d.get("skills") or [])[:12], "skill_areas": d.get("skill_domains") or [], "industry": d.get("industry") or None,
            "work_mode": d.get("work_mode"), "job_type": d.get("job_type"), "business_head": d.get("business_head")}


# ---------- the steps ----------

def check_input(run: audit.Run, name: str, data, text):
    """Intake checks and "What is it?". Returns (text, ext, verdict, failure)."""
    with tracing.span("intake checks"):
        if data is None:  # pasted into the chat
            text = str(text or "").strip()
            if not text:
                return None, None, None, _fail(run, "intake", "no job description text was given")
            if len(text) > MAX_PASTE:
                return None, None, None, _fail(run, "intake", f"the pasted text is longer than {MAX_PASTE} characters: attach it as a file")
            ext, has_images = ".txt", False
        else:
            ext = Path(name).suffix.lower()
            if ext not in JD_TYPES:
                return None, None, None, _fail(run, "intake", f"unsupported file type ({ext or 'no extension'}): send a PDF, DOCX, TXT or DOC file")
            if not data:
                return None, None, None, _fail(run, "intake", "empty file")
            if len(data) > settings.MAX_UPLOAD_BYTES:
                return None, None, None, _fail(run, "intake", f"file is larger than {settings.MAX_UPLOAD_LABEL}")
            text, has_images, err = documents.extract_text(data, ext)
            if err:
                return None, None, None, _fail(run, "intake", err)
    with tracing.span("what is it") as s:
        verdict = junk.classify(text, has_images)
        s.set(decision=verdict.decision, rule=verdict.signals["rule"], jd_signals=verdict.signals["jd"])
    return text, ext, verdict, None


async def parse_and_prepare(run: audit.Run, name: str, data, ext: str, text: str):
    """Reads the JD with Questlight's parser and resolves what it names (client, place, business head).
    Returns (details, failure)."""
    with tracing.span("parse JD", kind="llm") as s:
        s.set_usage(provider="questlight")
        parsed, err = await jobs.parse(name, data or b"", ext, text)
        if err:
            return None, _fail(run, "jd_parse", err)
    run.log("jd_parse", "ok", "read with Questlight's JD parser")
    details = jobs.from_parsed(parsed or {})
    with tracing.span("look up client, place, business head") as s:
        open_jobs, _ = await matching.fetch_open_jobs()
        notes = await jobs.prepare(details, open_jobs)
        s.set(client_found=bool(details.get("client_id")), place_found=bool(details.get("city_id")),
              business_head_found=bool(details.get("business_head_id")))
        if notes:
            s.set(problems=notes)
    return details, None


async def screen_top(run: audit.Run, job: dict, scan: dict) -> dict:
    """Puts the best candidates (up to JD_SCREEN_TOP) on the new job at the Screening stage, but only those who are a strong
    match themselves (score 40+): a weak third place is not screened just to make up the number.
    Returns {"status": screened | partial | failed | skipped, "message", "results"}."""
    top = [c for c in scan.get("candidates") or [] if c["score"] >= matching.WEAK_BELOW]
    out: dict
    if scan.get("status") != "ok" or not top:
        out = {"status": "skipped", "message": "no strong candidate, so nobody was put on the job", "results": []}
    else:
        with tracing.span("screen candidates"):
            results = []
            for c in top[:settings.JD_SCREEN_TOP]:
                r = await questlight.add_to_screening(c["id"], job["_id"], c["status"])
                results.append({"candidate": c["code"], "name": c["name"], "score": c["score"], **r})
        done = [r for r in results if r["status"] in ("screened", "already")]
        if len(done) == len(results):
            out = {"status": "screened", "message": f"{len(done)} candidate(s) put on the job at the Screening stage", "results": results}
        elif done:
            first = next(r["message"] for r in results if r["status"] not in ("screened", "already"))
            out = {"status": "partial", "message": f"{len(done)} of {len(results)} put on the job; the rest failed: {first}", "results": results}
        else:
            out = {"status": "failed", "message": f"nobody could be put on the job: {results[0]['message']}" if results else "nobody to screen",
                   "results": results}
    run.log("screen_candidates", out["status"], out["message"], record_id=job.get("jobId"),
            candidates=[{"candidate": r["candidate"], "status": r["status"]} for r in out["results"]])
    return out


async def create_and_scan(run: audit.Run, details: dict, allow_duplicate: bool = False) -> dict:
    """From a ready set of details: review, duplicate check, create the job, start Questlight's matching, rank and screen.
    Returns the "job" part of the result. Safe to call again after the recruiter supplied what was missing."""
    checked = jobs.review(details)
    out: dict = {"preview": preview(details), "adjusted": checked["adjusted"]}
    if checked["missing"]:
        run.log("create_job", "not_created", "required details are missing", missing=checked["missing"])
        return {**out, "status": "not_created", "message": "some details Questlight requires are missing, so no job was created yet",
                "missing_items": checked["items"]}
    body = checked["body"]

    if not allow_duplicate:
        with tracing.span("check duplicates") as s:
            dups, err = await jobs.check_duplicates(body)
            s.set(duplicates=len(dups))
            if err:
                s.set(problem=err)
        if dups:
            run.log("create_job", "duplicate_found", f"{len(dups)} similar open job(s)", duplicates=[x["job_id"] for x in dups])
            return {**out, "status": "duplicate_found", "duplicates": dups[:10],
                    "message": "Questlight already has an open job with this title for this client and city"}

    with tracing.span("create job") as s:
        created = await jobs.create_job(body)
        s.set(outcome=created["status"])
        if created["status"] == "failed":
            s.fail(created["message"])
    if created["status"] != "created":
        run.log("create_job", created["status"], created["message"])
        return {**out, "status": created["status"], "message": created["message"]}
    long_id, code = created.get("id"), created.get("job_id")
    if long_id and not code:  # some create answers carry only the long id: read the job back for its JOB- code
        found = await jobs.find_job(long_id)
        code = (found or {}).get("jobId")
    run.log("create_job", "created", "job created in Questlight", record_id=code, salary_given=bool(body["salaryRangeMax"]))
    out.update(status="created", message="job created in Questlight", job_id=code, id=long_id)
    if not long_id:
        out["message"] += " (Questlight's answer had no job id, so matching and screening were skipped: open the job in Questlight)"
        return out

    job = jobs.as_open_job(body, long_id, code, details)
    if matching._cache["jobs"]:
        matching._cache["jobs"].append(job)  # so resumes that come in next can be matched to it at once

    with tracing.span("start Questlight matching") as s:
        started = await jobs.start_matching(long_id)
        s.set(outcome=started["status"])
    run.log("job_matching", started["status"], started["message"], record_id=code)
    out["questlight_matching"] = started

    with tracing.span("find candidates") as s:
        scan = await candidates.find_for_job(job, max(settings.JD_SCREEN_TOP, 3))
        s.set(outcome=scan["status"])
    run.log("find_candidates_for_new_job", scan["status"], scan["message"], record_id=code,
            top=[{"candidate": c["code"], "score": c["score"]} for c in scan["candidates"]])
    out["candidates"] = scan
    out["screening"] = await screen_top(run, job, scan)
    return out


# ---------- the whole run ----------

async def process_jd(name: str, data, text: str = "", channel: str = audit.CHANNEL, state: dict | None = None,
                     run: audit.Run | None = None) -> dict:
    """Runs one JD through the whole intake. data is the file's bytes, or None for text pasted into the chat (then name is
    "pasted text"). run: pass the resume pipeline's run when a file it was given turned out to be a JD, so the file keeps
    one run id. state (optional) is filled with the run and the job details so the agent can follow up (supply what was
    missing, then create). Returns the result; result["kind"] is "resume" when it was a resume after all."""
    name = name or PASTED
    run = run or audit.Run(name, data if data is not None else (text or "").encode("utf-8"), channel)
    if state is not None:
        state["run"] = run
    trace = tracing.Trace(f"{run.run_id}-jd")
    try:
        with trace.span("jd intake", kind="chain") as root:
            root.set(channel=channel, source="pasted" if data is None else Path(name).suffix.lower() or "none")
            result = await _run(run, name, data, text, state)
            if not result["ok"]:
                root.fail(result["error"])
    finally:
        trace.save()
    result["trace"] = trace.summary()
    return result


async def _run(run: audit.Run, name: str, data, text, state) -> dict:
    text, ext, verdict, failure = check_input(run, name, data, text)
    if failure:
        return failure
    assert text is not None and ext is not None and verdict is not None  # check_input returns them unless it failed
    rule = verdict.signals["rule"]
    base = {"ok": True, "file": name, "reason": verdict.reason}
    if verdict.decision == "accepted":  # a resume after all: the caller sends it to the resume intake (nothing logged here)
        return {**base, "kind": "resume", "decision": "accepted"}
    run.log("jd_check", verdict.decision, verdict.reason, rule=rule, jd_signals=verdict.signals["jd"])
    if rule in junk.HARD_JUNK or rule == "scanned":
        return {**base, "kind": "junk", "decision": verdict.decision}
    # A file with no sign of a JD at all (a cover letter, an essay) is not sent to the parser on a guess. Pasted text is: the
    # recruiter said it is a JD, and a two-line brief ("Java dev, Pune, 5 yrs") has few signals.
    if data is not None and verdict.decision != "job_description" and not verdict.signals["jd"]:
        return {**base, "kind": "unclear", "decision": "needs_review",
                "reason": f"this doesn't look like a job description or a resume ({verdict.reason}): a person should look at it"}
    if state is not None:
        state.update(text=text, ext=ext)

    details, failure = await parse_and_prepare(run, name, data, ext, text)
    if failure or details is None:
        return failure or {}
    if state is not None:
        state["details"] = details
    job = await create_and_scan(run, details)
    return {**base, "kind": "job_description", "decision": "job_description", "job": job}
