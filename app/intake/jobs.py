"""The JD blocks: read a job description with Questlight's JD parser, turn it into a Questlight job, and create it.

The calls are the ones Questlight's own web app makes on its Create Job page (research notes: docs/notes.md, kept local):
  POST {PARSING_BASE_URL}/jd                       a JD (PDF file, or text) -> JSON. No login; AI inside Questlight.
  GET  /location/resolve-ids?country&state&city   place names -> the ids a job stores
  GET  /clients, GET /teams/leaders               who the client and the business head can be
  GET  /jobs/check-duplicates                     same title + client + city already open?
  POST /jobs/create                               the job (JSON)
  POST {MATCHING_BASE_URL}/semantic-score/job/id  start Questlight's own matching, as its UI does right after a create

Everything here apart from the parser is plain rules. The job is built in a "details" dict (one key per thing a recruiter
can supply); review() turns it into Questlight's create body and lists what is still missing, so the agent can ask.
Required by Questlight's API (found by probing with bodies it must reject): title, comments, summary, client, business
head, primary and assigned recruiters, openings, posting date, skill domains, primary skills, industry, min/max experience,
priority, a positive min and max salary, country/state/city ids and a status.
"""
import base64
import html
import json
import os
import re
import time
from collections import Counter
from datetime import date, datetime, timezone

import httpx

from app import settings
from app.intake import questlight
from app.observability import tracing

TIMEOUT_S = 60
CREATE_TIMEOUT_S = 150   # Questlight writes the job summary and screening questions with AI inside the create call
MATCHING_TIMEOUT_S = 20  # starting Questlight's matching; if it takes longer it is left running
CACHE_S = 600
TEXT_LIMIT = 20000       # characters of JD text sent to the parser
STATUS = "Active"        # a new job is open at once, so it can be matched and screened
PRIORITIES = ("High", "Medium", "Low")
WORK_MODES = {"remote": "Remote", "wfh": "Remote", "work from home": "Remote", "hybrid": "Hybrid",
              "on-site": "On-site", "onsite": "On-site", "on site": "On-site", "office": "On-site", "in-office": "On-site"}
JOB_TYPES = {"full-time": "Full-time", "full time": "Full-time", "fulltime": "Full-time", "permanent": "Full-time",
             "part-time": "Part-time", "part time": "Part-time", "contract": "Contract", "contractual": "Contract",
             "freelance": "Contract", "temporary": "Contract", "c2h": "Contract", "contract to hire": "Contract"}
# The location lookup knows only the current names of these cities.
CITY_NAMES = {"bangalore": "Bengaluru", "bombay": "Mumbai", "madras": "Chennai", "calcutta": "Kolkata",
              "gurgaon": "Gurugram", "mysore": "Mysuru", "trivandrum": "Thiruvananthapuram", "cochin": "Kochi",
              "vizag": "Visakhapatnam", "baroda": "Vadodara", "poona": "Pune", "mangalore": "Mangaluru",
              "pondicherry": "Puducherry", "allahabad": "Prayagraj", "belgaum": "Belagavi", "hubli": "Hubballi"}

# What the recruiter may be asked for, and how the question reads. (id -> what)
ASKS = {
    "title": "the job title",
    "summary": "a short description of the job",
    "client": "which client (customer) this job is for",
    "business_head": "the business head for this job",
    "location": "the job's city (and state, if it's not obvious)",
    "experience": "the experience range in years, like 4-8",
    "salary": "the salary range, like 12-18 LPA",
    "skills": "the main skills, comma-separated",
    "skill_domains": "the skill areas, like QA Automation, API Testing",
    "industry": "the industry, like IT or Banking",
}

_cache: dict = {"clients": (0.0, None), "leaders": (0.0, None), "places": {}}


# ---------- reading the JD ----------

def _parser_error(resp: httpx.Response) -> str:
    try:
        body = resp.json()
    except ValueError:
        return f"Questlight's JD parser returned HTTP {resp.status_code}"
    err = body.get("error") if isinstance(body, dict) else None
    msg = (err.get("message") if isinstance(err, dict) else err) or (body.get("message") if isinstance(body, dict) else None)
    return f"Questlight's JD parser couldn't read it: {msg}" if msg else f"Questlight's JD parser returned HTTP {resp.status_code}"


async def parse(name: str, data: bytes, ext: str, text: str):
    """Returns (parsed JD JSON, None) or (None, error message). A PDF goes up as the file, like Questlight's page does;
    anything else (DOCX, TXT, old .doc, pasted text) as the text we already read out of it."""
    form = {"jd_text": "" if ext == ".pdf" else text[:TEXT_LIMIT]}
    files = {"file": (name, data, "application/pdf")} if ext == ".pdf" else None
    from app.intake import parser_questlight  # the parsing service takes one request at a time; share its queue
    try:
        async with parser_questlight._slot:
            async with httpx.AsyncClient(timeout=httpx.Timeout(settings.PARSER_TIMEOUT_S)) as client:
                resp = await client.post(f"{settings.PARSING_BASE_URL}/jd", data=form, files=files)
    except httpx.TimeoutException:
        return None, f"Questlight's JD parser didn't answer within {settings.PARSER_TIMEOUT_S}s"
    except httpx.HTTPError as exc:
        return None, f"couldn't reach Questlight's JD parser ({type(exc).__name__})"
    tracing.current().set(http_status=resp.status_code)
    if resp.status_code != 200:
        return None, _parser_error(resp)
    try:
        body = resp.json()
    except ValueError:
        return None, "Questlight's JD parser sent back something that isn't JSON"
    if not isinstance(body, dict) or body.get("success") is False or body.get("error"):
        return None, _parser_error(resp)
    return body, None


# ---------- the details a job is built from ----------

def _str(value) -> str:
    return " ".join(str(value).split()) if value not in (None, "") else ""


def _num(value):
    """A number from 12, "12", "12.5", "1,200,000"; None for anything else (including "" and 0-like blanks)."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return value
    m = re.fullmatch(r"\s*(\d[\d,]*(?:\.\d+)?)\s*", str(value or ""))
    return float(m.group(1).replace(",", "")) if m else None


def _list(value) -> list:
    items = value if isinstance(value, list) else re.split(r"[,;\n]", str(value or ""))
    out = []
    for item in items:
        item = _str(item)
        if item and item.lower() not in (x.lower() for x in out):
            out.append(item)
    return out


def _date(value):
    try:
        return datetime.strptime(str(value)[:10], "%Y-%m-%d").date() if value else None
    except ValueError:
        return None


def from_parsed(parsed: dict) -> dict:
    """The JD parser's JSON -> the details a job is built from (one key per thing a recruiter could supply)."""
    p = parsed or {}
    degrees = _list(p.get("degree"))
    mode = WORK_MODES.get(_str(p.get("workMode")).lower())
    kind = JOB_TYPES.get(_str(p.get("jobType")).lower())
    return {
        "title": _str(p.get("jobPositionTitle")), "summary": _str(p.get("jobSummary")), "comments": _str(p.get("comments")),
        "client_name": _str(p.get("customerName")),
        "country": _str(p.get("country")), "state": _str(p.get("state")), "city": _str(p.get("city")),
        "work_address": _str(p.get("workAddress")),
        "min_exp": _num(p.get("minExperienceYears")), "max_exp": _num(p.get("maxExperienceYears")),
        "salary_min": _num(p.get("minimumSalary")), "salary_max": _num(p.get("maximumSalary")),
        "skills": _list(p.get("primarySkills")) or _list(p.get("skillsRequired")),
        "secondary_skills": [s for s in _list(p.get("skillsRequired")) if s.lower() not in (x.lower() for x in _list(p.get("primarySkills")))],
        "skill_domains": _list(p.get("skillDomains")), "industry": _str(p.get("industry")), "domain": _str(p.get("domain")),
        "degree": degrees[0] if degrees else "", "languages": _list(p.get("languages")),
        "job_type": kind, "work_mode": mode,
        "notice_months": _num(p.get("noticePeriodMonths")), "openings": _num(p.get("numberOfOpenings")),
        "closing_date": _date(p.get("closingDate")), "duration_months": _num(p.get("jobDurationMonths")),
        "work_hours": _num(p.get("jobWorkHoursPerDay")),
        "priority": _str(p.get("priority")).title(),
    }


# ---------- who and where: the lookups ----------

def _token_user():
    """The Questlight user the token belongs to (its JWT says so); jobs made here have them as the recruiter."""
    headers = questlight.auth_headers()
    if not headers:
        return None
    try:
        payload = headers["Authorization"].split()[1].split(".")[1]
        return json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4))).get("user_id")
    except (IndexError, ValueError, AttributeError):
        return None


async def _get(path: str, **params):
    """GET on Questlight's core API. Returns (data, error)."""
    headers = questlight.auth_headers()
    if not headers:
        return None, "QUESTLIGHT_TOKEN is not set"
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(TIMEOUT_S)) as client:
            resp = await client.get(f"{questlight.api_base()}{path}", headers=headers, params=params or None)
    except httpx.HTTPError as exc:
        return None, f"couldn't reach Questlight ({type(exc).__name__})"
    if resp.status_code == 401:
        return None, "Questlight rejected the token (tokens last 10 days): get a new one and update QUESTLIGHT_TOKEN"
    if resp.status_code != 200:
        return None, f"Questlight {path} returned HTTP {resp.status_code}"
    try:
        return resp.json()["data"], None
    except (ValueError, KeyError, TypeError):
        return None, f"Questlight {path} had an unexpected format"


async def _cached(key: str, path: str):
    at, value = _cache[key]
    if value is not None and time.time() - at < CACHE_S:
        return value, None
    data, err = await _get(path)
    if err:
        return [], err
    value = [x for x in data if isinstance(x, dict) and not x.get("isDeleted") and x.get("isActive", True) is not False] \
        if isinstance(data, list) else []
    _cache[key] = (time.time(), value)
    return value, None


def _clean_name(value) -> str:
    """Names from Questlight are shown to the recruiter, so markup and entity tricks are taken out (dev data has some)."""
    text = html.unescape(html.unescape(str(value or "")))
    return " ".join(re.sub(r"[<>`{}]", "", text).split())[:80]


def _usable(name: str) -> bool:
    return bool(name) and "javascript:" not in name.lower() and "%" not in name and "&#" not in name


async def clients():
    """[{id, name}] of the active clients, names cleaned."""
    rows, err = await _cached("clients", "/clients")
    return [{"id": c["_id"], "name": _clean_name(c.get("client_name"))} for c in rows
            if c.get("_id") and _usable(_clean_name(c.get("client_name")))], err


async def leaders():
    """[{id, name}] of the people who can be a job's business head (Questlight's team leaders)."""
    rows, err = await _cached("leaders", "/teams/leaders")
    return [{"id": u["_id"], "name": _clean_name(f"{u.get('firstName', '')} {u.get('lastName', '')}")} for u in rows
            if u.get("_id")], err


def _pick(options: list, wanted: str):
    """The one option whose name is `wanted` (exactly, ignoring case and punctuation), else the one that contains it or
    is contained in it. None when nothing or more than one fits."""
    norm = lambda s: re.sub(r"[^a-z0-9]+", " ", s.lower()).strip()  # noqa: E731
    want = norm(wanted)
    if not want:
        return None
    exact = [o for o in options if norm(o["name"]) == want]
    if len(exact) == 1:
        return exact[0]
    near = [o for o in options if want in norm(o["name"]) or (len(norm(o["name"])) >= 3 and norm(o["name"]) in want)]
    return near[0] if len(near) == 1 else None


async def resolve_place(country: str, state: str, city: str):
    """(country_id, state_id, city_id) for place names; any part can be None. Old city names are tried as their new ones."""
    key = (country.lower(), state.lower(), city.lower())
    if key in _cache["places"]:
        return _cache["places"][key]
    city = CITY_NAMES.get(city.lower(), city)
    data, err = await _get("/location/resolve-ids", country=country, state=state, city=city)
    if err or not isinstance(data, dict):
        return None, None, None
    ids = (data.get("country_id"), data.get("state_id"), data.get("city_id"))
    if ids[2] is None and state and city:  # a wrong or unusual state can hide a known city
        data, _ = await _get("/location/resolve-ids", country=country, state="", city=city)
        if isinstance(data, dict) and data.get("city_id"):
            ids = (data.get("country_id"), data.get("state_id"), data.get("city_id"))
    _cache["places"][key] = ids
    return ids


async def default_business_head(open_jobs: list, people: list):
    """QUESTLIGHT_BUSINESS_HEAD (a name or id) if set; else the team leader who heads most of the open jobs this token's
    user recruits for. None when neither gives an answer (the recruiter is then asked)."""
    wanted = os.getenv("QUESTLIGHT_BUSINESS_HEAD", "").strip()
    if wanted:
        return next((p for p in people if p["id"] == wanted), None) or _pick(people, wanted)
    me = _token_user()
    ids = {p["id"]: p for p in people}
    counts = Counter(j.get("businessHead") for j in open_jobs if j.get("primaryRecruiter") == me and j.get("businessHead") in ids)
    return ids[counts.most_common(1)[0][0]] if counts else None


async def prepare(details: dict, open_jobs: list) -> list:
    """Resolves what the JD names into Questlight ids (client, places) and picks the recruiter and the default business
    head. Fills details in place; returns notes on the lookups that failed (Questlight unreachable)."""
    notes = []
    details["recruiter_id"] = details.get("recruiter_id") or _token_user()
    if not details.get("client_id"):
        options, err = await clients()
        if err:
            notes.append(f"client list: {err}")
        wanted = details.get("client_name") or os.getenv("QUESTLIGHT_DEFAULT_CLIENT", "").strip()
        hit = _pick(options, wanted) if wanted else None
        if hit:
            details["client_id"], details["client_name"] = hit["id"], hit["name"]
    if not details.get("business_head_id"):
        people, err = await leaders()
        if err:
            notes.append(f"business heads: {err}")
        hit = await default_business_head(open_jobs, people)
        if hit:
            details["business_head_id"], details["business_head"] = hit["id"], hit["name"]
            details.setdefault("defaults", []).append(f"business head set to {hit['name']} (the usual one for your jobs)")
    if not details.get("city_id") and (details.get("city") or details.get("state")):
        details["country_id"], details["state_id"], details["city_id"] = await resolve_place(
            details.get("country") or "", details.get("state") or "", details.get("city") or "")
    return notes


# ---------- details -> Questlight's create body ----------

def _whole(x: float):
    return int(x) if float(x).is_integer() else round(x, 1)


def _paragraph(text: str) -> str:
    """Questlight shows these fields as rich text; the text is escaped so it can't carry markup."""
    return f"<p>{html.escape(text)}</p>" if text else ""


def review(details: dict) -> dict:
    """Builds Questlight's create body from the details. Returns {"body", "missing" (ids), "items" (what to ask),
    "adjusted" (what was filled in or corrected, to tell the recruiter)}. Nothing is sent anywhere."""
    d, adjusted, missing = details, list(details.get("defaults") or []), []
    title, summary = d.get("title") or "", d.get("summary") or ""
    if not title:
        missing.append("title")
    if not summary:
        missing.append("summary")
    if not d.get("client_id"):
        missing.append("client")
    if not d.get("business_head_id"):
        missing.append("business_head")
    if not d.get("city_id"):
        missing.append("location")

    lo, hi = 0.0, 0.0
    if d.get("min_exp") is None and d.get("max_exp") is None:
        missing.append("experience")
    else:
        lo = float(d["min_exp"]) if d.get("min_exp") is not None else 0.0
        if d.get("min_exp") is None:
            adjusted.append("no minimum experience given, saved as 0")
        hi = float(d["max_exp"]) if d.get("max_exp") is not None else lo + 5
        if d.get("max_exp") is None:
            adjusted.append(f"no maximum experience given, saved as {lo:g}-{hi:g} years")
        if hi < lo:
            lo, hi = hi, lo
            adjusted.append("the experience range was reversed, so it was swapped")

    smin, smax = 0.0, 0.0
    if not d.get("salary_min") and not d.get("salary_max"):
        missing.append("salary")
    else:
        smin = float(d.get("salary_min") or d["salary_max"])
        smax = float(d.get("salary_max") or d["salary_min"])
        if smax < smin:
            smin, smax = smax, smin
            adjusted.append("the salary range was reversed, so it was swapped")

    skills = d.get("skills") or []
    if not skills:
        missing.append("skills")
    domains = d.get("skill_domains") or []
    if not domains and d.get("domain"):
        domains = [d["domain"]]
        adjusted.append(f"skill area taken from the domain ({d['domain']})")
    if not domains:
        missing.append("skill_domains")
    if not d.get("industry"):
        missing.append("industry")

    openings = int(d["openings"]) if d.get("openings") and d["openings"] >= 1 else 1
    if not d.get("openings"):
        adjusted.append("number of openings not given, saved as 1")
    priority = d.get("priority") if d.get("priority") in PRIORITIES else "Medium"
    comments = d.get("comments") or ""
    if not comments and summary:
        comments = re.split(r"(?<=[.!?])\s", summary, maxsplit=1)[0]
        adjusted.append("no extra comments in the JD, so the first line of the summary is used")
    today = date.today()
    closing = d.get("closing_date")
    if closing and closing <= today:
        closing = None
        adjusted.append("the closing date in the JD has already passed, so it was left empty")

    body = {
        "jobPositionTitle": title[:150], "jobSummary": _paragraph(summary), "comments": _paragraph(comments),
        "clientId": d.get("client_id") or "", "businessHead": d.get("business_head_id") or "",
        "primaryRecruiter": d.get("recruiter_id") or "", "assignedRecruiters": [d["recruiter_id"]] if d.get("recruiter_id") else [],
        "salesManagers": [], "numberOfOpenings": openings,
        "postingDate": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z"),
        "skillDomain": domains[:10], "primarySkills": skills[:20], "industry": d.get("industry") or "",
        "minExperience": _whole(lo), "maxExperience": _whole(hi),
        "priority": priority, "salaryRangeMin": _whole(smin), "salaryRangeMax": _whole(smax),
        "country": d.get("country_id") or "", "state": d.get("state_id") or "", "city": d.get("city_id") or "",
        "jobStatus": STATUS,
    }
    optional = {"degree": d.get("degree"), "domain": d.get("domain"), "languages": d.get("languages") or None,
                "jobType": d.get("job_type"), "remoteJob": d.get("work_mode"), "workLocation": d.get("work_address"),
                "noticePeriod": d.get("notice_months"), "jobDuration": d.get("duration_months") or None,
                "jobWorkHours": d.get("work_hours") or None,
                "closingDate": f"{closing.isoformat()}T23:59:59.999Z" if closing else None}
    body.update({k: v for k, v in optional.items() if v not in (None, "", [])})
    if not d.get("recruiter_id"):  # can't be answered in the chat: the token itself is wrong
        missing.append("recruiter")
    items = [{"id": m, "what": ASKS.get(m, "the recruiter (QUESTLIGHT_TOKEN's user could not be read: check the token)"),
              "can_supply": m in ASKS} for m in missing]
    return {"body": body, "missing": missing, "items": items, "adjusted": adjusted}


# ---------- the recruiter's answers ----------

UNIT = {"lpa": 100000, "lakh": 100000, "lakhs": 100000, "lac": 100000, "lacs": 100000, "l": 100000,
        "cr": 10000000, "crore": 10000000, "crores": 10000000, "k": 1000, "thousand": 1000}


def parse_salary(text: str):
    """"12-18 LPA", "12 to 18 lakhs", "1200000 - 1800000", "20 LPA", "80k-1L" -> (min, max) in rupees, or None."""
    t = text.lower().replace(",", "")
    nums = [(float(n), (u or "").strip(".")) for n, u in re.findall(r"(\d+(?:\.\d+)?)\s*(lpa|lakhs?|lacs?|crores?|cr|k|thousand|l\b)?", t)]
    if not nums:
        return None
    unit = next((u for _, u in reversed(nums) if u), "")
    values = [n * UNIT.get(u or unit, 1) for n, u in nums[:2]]
    if not unit and max(values) < 200:  # bare small numbers in an Indian JD mean lakhs per annum
        values = [v * 100000 for v in values]
    lo, hi = (values[0], values[0]) if len(values) == 1 else sorted(values)
    return (round(lo), round(hi)) if hi > 0 else None


def parse_experience(text: str):
    """"4-8", "4 to 8 years", "5+", "at least 3" -> (min, max or None)."""
    nums = [float(n) for n in re.findall(r"\d+(?:\.\d+)?", text)]
    if not nums:
        return None
    if len(nums) >= 2:
        return min(nums[:2]), max(nums[:2])
    return nums[0], None


async def apply_answer(details: dict, key: str, value: str):
    """Stores one answer the recruiter gave. Returns None when applied, else why not (said back to the agent)."""
    value = _str(value)
    if not value:
        return "empty answer"
    if key == "title":
        details["title"] = value[:150]
    elif key == "summary":
        details["summary"] = value[:5000]
    elif key == "client":
        options, err = await clients()
        hit = _pick(options, value)
        if not hit:
            names = ", ".join(o["name"] for o in options[:25])
            return (err or f"no single client matches '{value[:60]}'") + (f"; the clients are: {names}" if names else "")
        details["client_id"], details["client_name"] = hit["id"], hit["name"]
    elif key == "business_head":
        people, err = await leaders()
        hit = _pick(people, value)
        if not hit:
            return (err or f"no single business head matches '{value[:60]}'") + "; they are: " + ", ".join(p["name"] for p in people)
        details["business_head_id"], details["business_head"] = hit["id"], hit["name"]
        details["defaults"] = [n for n in details.get("defaults") or [] if not n.startswith("business head")]
    elif key == "location":
        parts = [p.strip() for p in value.split(",") if p.strip()]
        city, state, country = (parts + ["", "", ""])[:3]
        ids = await resolve_place(country, state, city)
        if not ids[2]:
            return f"couldn't find the city '{city[:60]}' in Questlight's list: give the city as Questlight names it (e.g. Bengaluru)"
        details.update(city=CITY_NAMES.get(city.lower(), city), state=state, country=country,
                       country_id=ids[0], state_id=ids[1], city_id=ids[2])
    elif key == "experience":
        got = parse_experience(value)
        if not got:
            return "give the experience as numbers of years, like 4-8"
        details["min_exp"], details["max_exp"] = got
    elif key == "salary":
        got = parse_salary(value)
        if not got:
            return "give the salary as numbers, like 12-18 LPA"
        details["salary_min"], details["salary_max"] = got
    elif key == "skills":
        details["skills"] = _list(value)[:20]
    elif key == "skill_domains":
        details["skill_domains"] = _list(value)[:10]
    elif key == "industry":
        details["industry"] = value[:100]
    else:
        return f"unknown id '{key[:40]}': use the ids from missing_items"
    return None


# ---------- writing to Questlight ----------

async def check_duplicates(body: dict):
    """Open jobs Questlight thinks are the same (title + client + city), as [{job_id, title, client, city, status}]."""
    data, err = await _get("/jobs/check-duplicates", clientId=body["clientId"], city=body["city"], jobPositionTitle=body["jobPositionTitle"])
    if err or not isinstance(data, dict):
        return [], err
    return [{"job_id": x.get("jobId"), "title": _clean_name(x.get("jobPositionTitle")), "client": _clean_name(x.get("clientName")),
             "city": _clean_name(x.get("city")), "status": x.get("jobStatus")}
            for x in data.get("duplicates") or [] if isinstance(x, dict)], None


def _created_ids(body) -> tuple:
    """(long id, JOB- code) from the create answer, which comes in a few shapes."""
    data = body.get("data") if isinstance(body, dict) else None
    data = data.get("job", data) if isinstance(data, dict) else {}
    long_id = data.get("_id") or data.get("job_id") or data.get("id")
    code = data.get("jobId") if str(data.get("jobId") or "").startswith("JOB-") else None
    return long_id, code


async def create_job(body: dict) -> dict:
    """POST /jobs/create. Returns {"status": created | failed | skipped, "message", "id", "job_id"}."""
    headers = questlight.auth_headers()
    if not headers:
        return {"status": "skipped", "message": "QUESTLIGHT_TOKEN is not set, so no job was created"}
    with tracing.span("POST jobs/create") as s:
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(CREATE_TIMEOUT_S)) as client:
                resp = await client.post(f"{questlight.api_base()}/jobs/create", headers=headers, json=body)
        except httpx.TimeoutException:
            s.fail("timeout")
            return {"status": "failed", "message": f"Questlight didn't answer within {CREATE_TIMEOUT_S}s; the job may or may not "
                    "have been created, so check Questlight before trying again"}
        except httpx.HTTPError as exc:
            s.fail(type(exc).__name__)
            return {"status": "failed", "message": f"couldn't reach Questlight ({type(exc).__name__})"}
        s.set(http_status=resp.status_code)
        if resp.status_code in (200, 201):
            try:
                long_id, code = _created_ids(resp.json())
            except ValueError:
                long_id, code = None, None
            s.set(outcome="created")
            return {"status": "created", "message": "job created in Questlight", "id": long_id, "job_id": code}
        problem = _problem(resp)
        s.fail(problem)
        return {"status": "failed", "message": problem}


def _problem(resp: httpx.Response) -> str:
    if resp.status_code == 401:
        return "Questlight rejected the token (tokens last 10 days): get a new one and update QUESTLIGHT_TOKEN"
    if resp.status_code == 403:
        return "Questlight refused: the account needs permission to create jobs (JOBS:CREATE)"
    try:
        body = resp.json()
    except ValueError:
        return f"Questlight returned HTTP {resp.status_code}"
    msgs = body.get("errorMessage") if isinstance(body, dict) else None
    if isinstance(msgs, list):
        return f"Questlight returned HTTP {resp.status_code}: " + "; ".join(str(m) for m in msgs[:8])
    return f"Questlight returned HTTP {resp.status_code}: {str((body or {}).get('message') or msgs)[:300]}"


async def find_job(long_id: str | None = None, code: str | None = None):
    """The job as Questlight's job list holds it, read back after a create (the create answer may not carry the code)."""
    data, err = await _get(f"/jobs/{long_id or code}")
    if err or not isinstance(data, dict):
        return None
    return data


async def start_matching(long_id: str) -> dict:
    """Starts Questlight's own matching for the new job, as its Create Job page does. Its results land in the job's
    Matching Profiles tab later. Returns {"status": started | failed, "message"}."""
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(MATCHING_TIMEOUT_S)) as client:
            resp = await client.post(f"{settings.MATCHING_BASE_URL}/semantic-score/job/{long_id}", headers=questlight.auth_headers() or {})
    except httpx.TimeoutException:
        return {"status": "started", "message": f"Questlight's matching is still running after {MATCHING_TIMEOUT_S}s; its "
                "results will show in the job's Matching Profiles tab"}
    except httpx.HTTPError as exc:
        return {"status": "failed", "message": f"couldn't reach Questlight's matching service ({type(exc).__name__})"}
    if resp.status_code in (200, 201, 202):
        return {"status": "started", "message": "Questlight's matching started; its results show in the job's Matching Profiles tab"}
    return {"status": "failed", "message": f"Questlight's matching service returned HTTP {resp.status_code}"}


def as_open_job(body: dict, long_id: str, code: str | None, details: dict) -> dict:
    """The new job in the shape of GET /jobs/all, so our own matching and candidate ranking can use it at once."""
    return {**body, "_id": long_id, "jobId": code, "isDeleted": False, "secondarySkills": details.get("secondary_skills") or []}
