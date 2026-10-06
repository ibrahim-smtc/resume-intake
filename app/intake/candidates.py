"""The "Find candidates for a job" block: the reverse of matching. Given an open job, ranks the candidates already in
Questlight and returns the best few; a recruiter can then put them on the job at the Screening stage.

Candidates come from GET /applicants/findAll (the call behind Questlight's Candidates page): full profiles, 100 per page.
All pages are fetched in parallel (769 candidates take about 3 seconds) and kept 10 minutes. Only what scoring needs is
kept, never contact details, date of birth or the password-style `otp` field the API also returns.

Scoring is the same as for resume -> jobs (matching.score_fit): hardcoded rules, no AI. Candidates who are already hired or
being onboarded are left out, and so are the ones already on the job (GET /screening/job/{id}/existing-applicants, the call
behind the job's own screening list).
"""
import asyncio
import time
from collections import Counter

import httpx

from app.intake import matching, questlight
from app.observability import tracing

EXCLUDED_STATUSES = {"PLACED_WITH_US", "ONBOARDING_IN_PROCESS", "READY_FOR_ONBOARDING"}  # already hired or on their way
TOP_K = 3
MAX_K = 10
MAX_ADD = 10          # most candidates one add-to-job call may carry
PAGE_SIZE = 100       # Questlight rejects 500
MAX_PAGES = 60        # 6000 candidates: a safety stop, reported when reached
PARALLEL_PAGES = 8
CACHE_S = 600
TIMEOUT_S = 60

_cache = {"at": 0.0, "people": [], "truncated": False}
_lock = asyncio.Lock()


def _flat(value) -> str:
    """Questlight stores responsibilities as a list of sentences; accept a plain string too."""
    return " ".join(str(v) for v in value) if isinstance(value, list) else str(value or "")


def _slim(a: dict):
    """What scoring needs from a Questlight candidate, or None for one that is deleted or has no ID."""
    if not isinstance(a, dict) or not a.get("_id") or a.get("isDeleted") or a.get("deleted_at"):
        return None
    work = [w for w in a.get("workExperience") or [] if isinstance(w, dict)]
    exp = a.get("total_experience") if isinstance(a.get("total_experience"), dict) else {}
    return {"id": a["_id"], "code": a.get("applicantId"), "name": str(a.get("name") or "").strip(),
            "status": str(a.get("status") or ""), "address": str(a.get("address") or ""),
            "skills": [s for s in a.get("skills") or [] if isinstance(s, str)],
            "titles": [str(w["jobTitle"]).strip() for w in work if w.get("jobTitle")],
            "work_text": " ".join(_flat(w.get("responsibilities")) for w in work),
            "degrees": [str(e["degree"]) for e in a.get("education") or [] if isinstance(e, dict) and e.get("degree")],
            "years": (exp.get("years") or 0) + (exp.get("months") or 0) / 12}


async def _page(client: httpx.AsyncClient, headers: dict, number: int):
    """Returns (the page's "data" object, error message)."""
    try:
        resp = await client.get(f"{questlight.api_base()}/applicants/findAll", headers=headers,
                                params={"limit": PAGE_SIZE, "page": number, "columnFilters": "%5B%5D", "sorting": "%5B%5D", "searchTerm": ""})
    except httpx.TimeoutException:
        return None, f"Questlight's candidate list didn't answer within {TIMEOUT_S}s"
    except httpx.HTTPError as exc:
        return None, f"couldn't reach Questlight ({type(exc).__name__}: {exc or 'no detail'})"
    if resp.status_code == 401:
        return None, "Questlight rejected the token (tokens last 10 days): get a new one and update QUESTLIGHT_TOKEN"
    if resp.status_code != 200:
        return None, f"Questlight's candidate list returned HTTP {resp.status_code}"
    try:
        data = resp.json()["data"]
        if not isinstance(data["data"], list):
            raise TypeError
        return data, None
    except (ValueError, KeyError, TypeError):
        return None, "Questlight's candidate list had an unexpected format"


async def _download():
    headers = questlight.auth_headers()
    if not headers:
        return [], False, "QUESTLIGHT_TOKEN is not set"
    async with httpx.AsyncClient(timeout=httpx.Timeout(TIMEOUT_S)) as client:
        first, err = await _page(client, headers, 1)
        if err:
            return [], False, err
        total_pages = first.get("totalPages") if isinstance(first.get("totalPages"), int) else 1
        pages = max(1, min(total_pages, MAX_PAGES))
        gate = asyncio.Semaphore(PARALLEL_PAGES)

        async def fetch(n):
            async with gate:
                return await _page(client, headers, n)

        rest = await asyncio.gather(*[fetch(n) for n in range(2, pages + 1)])
    rows = list(first["data"])
    for data, err in rest:
        if err:  # a missing page would silently drop candidates, so any failure fails the whole list
            return [], False, err
        rows += data["data"]
    return [p for p in map(_slim, rows) if p], total_pages > MAX_PAGES, None


async def fetch_candidates():
    """Returns (candidates, error message). Cached for CACHE_S seconds."""
    with tracing.span("fetch candidates") as s:
        async with _lock:  # two questions at once on a cold cache download the list once
            cached = bool(_cache["people"]) and time.time() - _cache["at"] < CACHE_S
            if not cached:
                people, truncated, err = await _download()
                if err:
                    s.fail(err)
                    return [], err
                _cache.update(at=time.time(), people=people, truncated=truncated)
        s.set(from_cache=cached, candidates=len(_cache["people"]))
        return _cache["people"], None


# ---------- ranking ----------

def _words_of(person: dict) -> str:
    return " ".join([" ".join(person["titles"]), person["work_text"], " ".join(person["skills"])])


def _doc(person: dict):
    """A candidate as text for the index, the same way matching.job_text does it for a job."""
    body = " ".join([" ".join(person["skills"]), person["work_text"], " ".join(person["degrees"])])
    return Counter(matching.words(" ".join([" ".join(person["titles"][:3])] * 3 + [body]))), len(matching.words(body))


def rank(job: dict, people: list, top_k: int = TOP_K):
    """Scores every eligible candidate for this job. Returns (best top_k, number left out for being hired/onboarding)."""
    eligible = [p for p in people if p["status"] not in EXCLUDED_STATUSES]
    if not eligible:
        return [], len(people)
    query = Counter(matching.words(" ".join([str(job.get("jobPositionTitle") or "")] * 3 + [
        " ".join(map(str, job.get("primarySkills") or [])), matching.plain(job.get("jobSummary")),
        matching.plain(job.get("additionalDetails"))])))
    sims = list(matching.TextIndex([_doc(p) for p in eligible]).similarity(query))
    scored = []
    for p, sim in zip(eligible, sims):
        fit = matching.score_fit(job, sim, _words_of(p).lower(), p["years"], p["titles"][:3], p["degrees"], p["address"])
        scored.append({"id": p["id"], "code": p["code"], "name": p["name"], "status": p["status"], "location": p["address"], **fit})
    scored.sort(key=lambda c: c["score"], reverse=True)
    return scored[:top_k], len(people) - len(eligible)


async def on_job(job_id: str | None):
    """The long ids of the candidates already on this job (any stage). Returns (set of ids, error message)."""
    headers = questlight.auth_headers()
    if not headers or not job_id:
        return set(), "QUESTLIGHT_TOKEN is not set" if not headers else "the job has no id"
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(TIMEOUT_S)) as client:
            resp = await client.get(f"{questlight.api_base()}/screening/job/{job_id}/existing-applicants", headers=headers)
    except httpx.HTTPError as exc:
        return set(), f"couldn't read who is already on the job ({type(exc).__name__})"
    if resp.status_code != 200:
        return set(), f"couldn't read who is already on the job (HTTP {resp.status_code})"
    try:
        data = resp.json()["data"]
    except (ValueError, KeyError, TypeError):
        return set(), "couldn't read who is already on the job (unexpected format)"
    return {str(x.get("_id") if isinstance(x, dict) else x) for x in data or []}, None


async def find_for_job(job: dict, top_k: int = TOP_K) -> dict:
    """Returns {"status": ok | no_strong_match | failed, "message", "considered", "left_out", "already_on_job",
    "candidates"}. Candidates already on the job are not suggested (if that list can't be read, "on_job_check" says so).
    The message never holds a name: it goes to the audit log."""
    people, err = await fetch_candidates()
    if err:
        return {"status": "failed", "message": err, "considered": 0, "left_out": 0, "already_on_job": 0, "candidates": []}
    top_k = max(1, min(int(top_k), MAX_K))
    with tracing.span("score candidates") as s:
        there, check_err = await on_job(job.get("_id"))
        pool = [p for p in people if p["id"] not in there]
        best, left_out = rank(job, pool, top_k)
        considered = len(pool) - left_out
        s.set(candidates_scored=considered, left_out=left_out, already_on_job=len(people) - len(pool),
              best_score=best[0]["score"] if best else None)
    note = " (only the first %d candidates were read)" % (MAX_PAGES * PAGE_SIZE) if _cache["truncated"] else ""
    out = {"considered": considered, "left_out": left_out, "already_on_job": len(people) - len(pool), "candidates": best}
    if check_err:
        out["on_job_check"] = check_err + ": a suggested candidate may already be on the job"
    if not best:
        return {"status": "failed", **out, "message": "there are no candidates to rank: everyone is hired, in onboarding, "
                "or already on the job" + note}
    if best[0]["score"] < matching.WEAK_BELOW:
        return {"status": "no_strong_match", **out, "message": f"no strong candidate among {considered} (best score is "
                f"{best[0]['score']}, under {matching.WEAK_BELOW}){note}"}
    return {"status": "ok", **out, "message": f"best score {best[0]['score']} out of {considered} candidates considered, "
            f"{left_out} left out as hired or in onboarding, {out['already_on_job']} already on the job{note}"}


# ---------- putting candidates on the job ----------

def pick(refs, people: list):
    """Finds each candidate the recruiter named, by candidate ID (CAN-...) or long ID. Returns (found, problems)."""
    by_ref = {}
    for p in people:
        by_ref[str(p["id"]).lower()] = p
        if p["code"]:
            by_ref[str(p["code"]).lower()] = p
    found, problems, seen = [], [], set()
    for ref in refs:
        p = by_ref.get(str(ref).strip().lower())
        if not p:
            problems.append({"candidate": str(ref)[:40], "status": "not_found", "message": "no candidate with this ID"})
        elif p["id"] in seen:
            continue
        elif p["status"] in EXCLUDED_STATUSES:
            problems.append({"candidate": p["code"], "status": "refused", "message": "already hired or in onboarding, so not added to a job from here"})
        else:
            seen.add(p["id"])
            found.append(p)
    return found, problems


async def add_to_job(job: dict, found: list) -> list:
    """Puts each candidate on the job at the Screening stage, one at a time. Returns [{"candidate", "status", "message"}]."""
    results = []
    for p in found:
        r = await questlight.add_to_screening(p["id"], job["_id"], p["status"])
        results.append({"candidate": p["code"], **r})
    return results
