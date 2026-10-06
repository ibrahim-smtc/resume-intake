"""The "Match open roles" block: ranks Questlight's open jobs against a newly created candidate.

Why this isn't Questlight's own matching API: on dev, POST /matching/semantic-score/resume/{id} fails with a server error
(it is missing a tenant_id argument), and the job-side GET only lists scores that a background process has already
computed (top 25 per job), so a brand-new resume is not in them. So the scoring is done here, from the job data
(GET /api/jobs/all) and the resume. Hardcoded rules, no LLM, no Perfox.

Score, 0-100, from six parts (WEIGHTS below; these weights are my starting point, not tuned on real outcomes):
  skills      the job's primary (and half-weight secondary) skills found in the resume
  text        TF-IDF similarity between the resume text and the job's title, skills and description
  title       how many words of the job title appear in the candidate's recent job titles
  experience  the candidate's total years against the job's min/max
  degree      the job's degree requirement against the candidate's degrees
  location    the candidate's address against the job's city, state and work location

"Open" job = jobStatus Active or Open, not deleted, and not past its closing date.
"""
import html
import math
import re
import time
from collections import Counter
from datetime import date

import httpx

from app.intake import questlight
from app.observability import tracing

OPEN_STATUSES = {"active", "open"}  # the dev data uses both
TOP_N = 3
WEAK_BELOW = 40  # best score under this is reported as "no strong match"
WEIGHTS = {"skills": 40, "text": 20, "title": 15, "experience": 15, "degree": 5, "location": 5}
TEXT_FULL = 0.30  # a TF-IDF cosine this high counts as a full text match
THIN_SKILLS = 3  # jobs with fewer skills than this get proportionally less skill credit
THIN_TEXT_WORDS = 25  # jobs with fewer description words than this get proportionally less text credit
CACHE_S = 600  # the job list is big, so it is kept for 10 minutes
TIMEOUT_S = 60

STOP = set("""a an and are as at be by for from has have in is it of on or that the this to was were will with you your our
we they their them he she his her who what which about into over per via using use used work working role roles
resource resources requirement requirements position positions hiring urgent opening openings job jobs candidate
experience experienced years year strong good knowledge skills skill ability responsible responsibilities""".split())

_cache = {"at": 0.0, "jobs": []}


def plain(text) -> str:
    return html.unescape(re.sub(r"<[^>]+>", " ", str(text or "")))


def words(text) -> list:
    return [w for w in re.findall(r"[a-z0-9][a-z0-9+#.]*", plain(text).lower().replace(" ", " "))
            if len(w) > 1 and w not in STOP]


def _has_phrase(haystack: str, phrase: str) -> bool:
    return bool(re.search(r"(?<![a-z0-9+#])" + re.escape(phrase) + r"(?![a-z0-9+#])", haystack))


def _is_open(job: dict, today: date) -> bool:
    if job.get("isDeleted") or str(job.get("jobStatus", "")).strip().lower() not in OPEN_STATUSES:
        return False
    closing = str(job.get("closingDate") or "")[:10]
    return not (closing[:4] >= "2000" and closing < today.isoformat())  # 1970 means "never set"


async def fetch_open_jobs():
    """Returns (open jobs, error message). Cached for CACHE_S seconds."""
    with tracing.span("fetch open jobs") as s:
        cached = bool(_cache["jobs"]) and time.time() - _cache["at"] < CACHE_S
        jobs, err = (_cache["jobs"], None) if cached else await _download_open_jobs()
        s.set(from_cache=cached, open_jobs=len(jobs))
        if err:
            s.fail(err)
        return jobs, err


async def _download_open_jobs():
    headers = questlight.auth_headers()
    if not headers:
        return [], "QUESTLIGHT_TOKEN is not set"
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(TIMEOUT_S)) as client:
            resp = await client.get(f"{questlight.api_base()}/jobs/all", headers=headers)
    except httpx.TimeoutException:
        return [], f"Questlight's job list didn't answer within {TIMEOUT_S}s"
    except httpx.HTTPError as exc:
        return [], f"couldn't reach Questlight ({type(exc).__name__}: {exc or 'no detail'})"
    tracing.current().set(http_status=resp.status_code)
    if resp.status_code != 200:
        return [], f"Questlight's job list returned HTTP {resp.status_code}"
    try:
        jobs = resp.json()["data"]
    except (ValueError, KeyError, TypeError):
        return [], "Questlight's job list had an unexpected format"
    today = date.today()
    _cache.update(at=time.time(), jobs=[j for j in jobs if _is_open(j, today)])
    tracing.current().set(all_jobs=len(jobs))
    return _cache["jobs"], None


# ---------- the six scores, each 0..1 ----------

def _skills(job: dict, haystack: str):
    primary = [s.strip() for s in job.get("primarySkills") or [] if isinstance(s, str) and len(s.strip()) > 1]
    secondary = [s.strip() for s in job.get("secondarySkills") or [] if isinstance(s, str) and len(s.strip()) > 1]
    weighted = [(s, 1.0) for s in primary] + [(s, 0.5) for s in secondary]
    if not weighted:
        return 0.5, [], []
    hit = [(s, w) for s, w in weighted if _has_phrase(haystack, s.lower())]
    total = sum(w for _, w in weighted)
    # a job that lists one skill can't prove much: matching 1 of 1 counts for a third of a full 3-skill match
    got = sum(w for _, w in hit) / total * min(1.0, total / THIN_SKILLS)
    return got, [s for s, _ in hit], [s for s in primary if not _has_phrase(haystack, s.lower())]


TITLE_SAME = {"engineer": "developer", "programmer": "developer", "coder": "developer", "swe": "developer",
              "sr": "senior", "jr": "junior", "mgr": "manager", "exec": "executive"}


def _title_words(text) -> set:
    return {TITLE_SAME.get(w, w) for w in words(text)} - {"senior", "junior", "lead", "associate"}


def _title(job: dict, titles: list) -> float:
    want = _title_words(job.get("jobPositionTitle"))
    if not want or not titles:
        return 0.0
    return max(len(want & _title_words(t)) / len(want) for t in titles)


def _experience(job: dict, years: float):
    lo, hi = job.get("minExperience"), job.get("maxExperience")
    lo = lo if isinstance(lo, (int, float)) else 0
    hi = hi if isinstance(hi, (int, float)) else 0
    if not lo and not hi:
        return 0.5, "the job doesn't state a range"
    hi = hi or float("inf")
    if lo <= years <= hi:
        return 1.0, f"{years:.0f} yrs fits {lo}-{hi if hi != float('inf') else '+'}"
    if years < lo:
        return max(0.0, 1 - (lo - years) / max(lo, 1)), f"{years:.0f} yrs is under the {lo} wanted"
    return max(0.5, 1 - 0.1 * (years - hi)), f"{years:.0f} yrs is over the {hi:.0f} maximum"


def _degree(job: dict, degrees: list) -> float:
    raw = str(job.get("degree") or "").lower()
    if not raw.strip():
        return 0.5
    if not degrees:
        return 0.0
    if "graduate" in raw:  # "Graduate" / "Any graduate": any degree will do
        return 1.0
    wanted = {re.sub(r"[.\s]", "", t) for t in re.split(r"[/,;&|]|\bor\b|\band\b", raw)} - {""}
    have = {re.sub(r"[.\s]", "", d.lower()) for d in degrees}
    # "btech" matches "btechincomputerscience"; very short codes ("be") must match exactly
    return 1.0 if any(w == h or (len(w) >= 3 and w in h) or (len(h) >= 3 and h in w) for w in wanted for h in have) else 0.0


def _location(job: dict, address: str) -> float:
    # some jobs store numeric codes instead of place names; those can't be compared
    where = {w for w in words(" ".join(str(job.get(k) or "") for k in ("city", "state", "workLocation")))
             if not w.isdigit()} - {"india"}
    if not where:
        return 0.5
    mine = set(words(address)) - {"india"}
    if not mine:
        return 0.5
    return 1.0 if where & mine else 0.0


def job_text(job: dict):
    """A job as text for the index: (its words with the title counted 3 times, how many words it says apart from the title)."""
    body = " ".join([" ".join(map(str, job.get("primarySkills") or [])), plain(job.get("jobSummary")),
                     plain(job.get("additionalDetails"))])
    return Counter(words(" ".join([str(job.get("jobPositionTitle") or "")] * 3 + [body]))), len(words(body))


class TextIndex:
    """TF-IDF over a set of documents (the open jobs, or the candidates), so words that appear in nearly every document
    ("team", "development") count for little. docs: (word counts, size) pairs; size is how much the document actually
    says apart from its title, because a one-line document can't match on text."""

    def __init__(self, docs: list):
        self.docs = [d for d, _ in docs]
        self.sizes = [size for _, size in docs]
        df = Counter(w for d in self.docs for w in d)
        n = len(self.docs)
        self.idf = {w: math.log((1 + n) / (1 + c)) + 1 for w, c in df.items()}
        self.norms = [math.sqrt(sum((self._tf(c) * self.idf[w]) ** 2 for w, c in d.items())) or 1 for d in self.docs]

    @staticmethod
    def _tf(count: int) -> float:
        return 1 + math.log(count)

    def similarity(self, resume_words: Counter):
        q = {w: self._tf(c) * self.idf[w] for w, c in resume_words.items() if w in self.idf}
        qn = math.sqrt(sum(v * v for v in q.values())) or 1
        for d, norm, size in zip(self.docs, self.norms, self.sizes):
            cosine = sum(v * self._tf(d[w]) * self.idf[w] for w, v in q.items() if w in d) / (qn * norm)
            yield cosine * min(1.0, size / THIN_TEXT_WORDS)  # a job with a one-line description can't match on text


def score_fit(job: dict, text_sim: float, haystack: str, years: float, titles: list, degrees: list, address: str) -> dict:
    """How well one candidate fits one job, 0-100, from the six parts. This is the whole scoring: resume -> jobs (rank below)
    and job -> candidates (candidates.py) both call it. text_sim is the TF-IDF similarity from a TextIndex; haystack is the
    candidate's lower-cased text and skills."""
    s_skills, matched, missing = _skills(job, haystack)
    s_exp, exp_note = _experience(job, years)
    parts = {"skills": s_skills, "text": min(1.0, text_sim / TEXT_FULL), "title": _title(job, titles),
             "experience": s_exp, "degree": _degree(job, degrees), "location": _location(job, address)}
    return {"score": round(sum(WEIGHTS[k] * v for k, v in parts.items()), 1), "matchedSkills": matched, "missingSkills": missing,
            "experience": exp_note, "parts": {k: round(v * WEIGHTS[k], 1) for k, v in parts.items()}}


def find_job(ref: str, jobs: list):
    """Finds the one open job a recruiter means, by its job ID (JOB-...), its long ID or its title. Returns (job, choices):
    the job when exactly one fits, else None with the jobs that fit (empty when nothing does, so the recruiter can pick)."""
    want = " ".join(str(ref or "").lower().split())
    if not want:
        return None, []
    for j in jobs:
        if want in (str(j.get("jobId") or "").lower(), str(j.get("_id") or "").lower()):
            return j, []
    title = lambda j: " ".join(str(j.get("jobPositionTitle") or "").lower().split())
    same = [j for j in jobs if title(j) == want]
    if len(same) == 1:
        return same[0], []
    found = same or [j for j in jobs if all(_has_phrase(title(j), w) for w in want.split())]
    return (found[0], []) if len(found) == 1 else (None, found)


# ---------- the block ----------

def rank(jobs: list, parsed: dict, resume_text: str, top_n: int = TOP_N) -> list:
    """Scores every job for this candidate and returns the best top_n, best first."""
    skills = [s for s in (parsed.get("technical_skills") or []) + (parsed.get("skills") or []) if isinstance(s, str)]
    haystack = (resume_text.lower() + "\n" + "\n".join(s.lower() for s in skills))
    exp = parsed.get("total_experience") or {}
    years = (exp.get("years") or 0) + (exp.get("months") or 0) / 12
    titles = [j.get("jobTitle") for j in (parsed.get("workExperience") or [])[:3] if j.get("jobTitle")]
    degrees = [e.get("degree") for e in parsed.get("education") or [] if e.get("degree")]
    address = str(parsed.get("address") or "")
    sims = list(TextIndex([job_text(j) for j in jobs]).similarity(Counter(words(resume_text))))

    scored = []
    for job, sim in zip(jobs, sims):
        fit = score_fit(job, sim, haystack, years, titles, degrees, address)
        scored.append({"jobId": job.get("jobId"), "id": job.get("_id"), "title": str(job.get("jobPositionTitle") or "").strip(),
                       **fit, "location": ", ".join(str(job[k]) for k in ("city", "state") if job.get(k) and not str(job[k]).isdigit())})
    scored.sort(key=lambda m: m["score"], reverse=True)
    return scored[:top_n]


async def match_roles(parsed: dict, resume_text: str) -> dict:
    """Returns {"status": ok | no_strong_match | failed, "message", "open_jobs", "matches"}."""
    jobs, err = await fetch_open_jobs()
    if err:
        return {"status": "failed", "message": err, "open_jobs": 0, "matches": []}
    if not jobs:
        return {"status": "failed", "message": "Questlight has no open jobs to match against", "open_jobs": 0, "matches": []}
    with tracing.span("score jobs") as s:
        matches = rank(jobs, parsed, resume_text)
        s.set(jobs_scored=len(jobs), best_score=matches[0]["score"])
    if matches[0]["score"] < WEAK_BELOW:
        return {"status": "no_strong_match", "open_jobs": len(jobs), "matches": matches,
                "message": f"no strong match among {len(jobs)} open jobs (best is {matches[0]['score']}, under {WEAK_BELOW})"}
    return {"status": "ok", "open_jobs": len(jobs), "matches": matches,
            "message": f"best match: {matches[0]['title']} ({matches[0]['score']}) out of {len(jobs)} open jobs"}
