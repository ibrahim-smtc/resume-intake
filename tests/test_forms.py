"""The fill-in form for what a resume lacks (app/agent/forms.py): the agent's result carries its private link; the form is a Pydantic
model built from the file's missing details (plus what was read, to correct); submitting it creates the profile with the agent's
own step tools; skip, a resume read only once its contact details are given, expiry, and the form skipping the app's password.
Parser and Questlight are fake; the job list is hand-made."""
import asyncio
import json
import os

import harness

harness.isolate(PUBLIC_BASE_URL="https://intake.example")
from harness import check, finish  # noqa: E402

import agent_harness as ah  # noqa: E402
from fixtures.parsed import HARISH, ROHIT, variant  # noqa: E402

NO_CONTACT = ("We couldn't find the following details in this resume: email, phoneNumber. "
              "Please make sure it clearly shows the name, email, and phone number.")
FULL = {**ROHIT, "name": "Parsed Name", "email": "parsed@example.com"}
fakes = ah.start({"harish": HARISH, "skipme": HARISH, "noskills": variant(ROHIT, technical_skills=[], soft_skills=[]),
                  "noedu": variant(ROHIT, education=[]), "incomplete": {"until_contact": NO_CONTACT, "then": FULL},
                  "rohit": ROHIT, "twice": HARISH})

import httpx  # noqa: E402

from app.agent import forms, intakes  # noqa: E402

STEMS = ("harish", "skipme", "noskills", "noedu", "incomplete", "rohit", "twice")


async def take_in():
    async with ah.session(fakes) as s:
        return {stem: await ah.call(s, "process_resume", file_url=f"https://files.test/{stem}.pdf") for stem in STEMS}


r = asyncio.run(take_in())
web = httpx.Client(base_url=fakes.server.url, timeout=60)


def token(stem):
    return r[stem]["form_url"].rsplit("/", 1)[-1]


def get(stem):
    resp = web.get(f"/api/form/{token(stem)}")
    return resp.status_code, resp.json()


def post(stem, values=None, skip=False):
    resp = web.post(f"/api/form/{token(stem)}", json={"values": values or {}, "skip": skip})
    return resp.status_code, resp.json()


def props(form):
    return form["schema"]["properties"]


# ---- the agent's result carries the link ----
h = r["harish"]
check("a resume with something missing gets a form link on the public address", h["form_url"].startswith("https://intake.example/fill/")
      and len(token("harish")) >= 30, h.get("form_url"))
check("...and the agent is told to offer it, with the chat route still described", "form_url" in h["next"] and "provide_missing_details" in h["next"], h["next"])
check("a complete resume gets no form", "form_url" not in r["rohit"] and r["rohit"]["profile_created"], r["rohit"].get("profile"))
check("each file gets its own form", len({token(s) for s in ("harish", "skipme", "twice")}) == 3)

# ---- the form: a schema built from the missing details plus what was read ----
code, f = get("harish")
p = props(f)
check("the form is open, for that file", code == 200 and f["status"] == "open" and f["file"] == "harish.pdf", (code, f.get("status"), f.get("file")))
check("the two missing job titles are required fields, named by their ids and saying which job",
      f["schema"]["required"] == ["job_title:1", "job_title:2"] and "Walmart Global Tech India" in p["job_title:1"]["description"]
      and p["job_title:1"]["group"] == "missing", (f["schema"].get("required"), p.get("job_title:1")))
check("what was read is there to correct, filled in: name, email, phone, location",
      p["name"]["default"] == "Harish Ramalingam" and p["email"]["default"] == "harish.test@example.com" and p["phone"]["input"] == "tel"
      and p["address"]["default"] == "" and all(p[k]["group"] == "read" for k in ("name", "email", "phone", "address")), p.get("name"))
check("skills can be added, and the form says which were read", p["skills"]["default"] == "" and "Python, Scala, SQL" in p["skills"]["description"], p.get("skills"))
check("missing items are listed first, and the form can be skipped (only job titles are missing)",
      list(p)[:2] == ["job_title:1", "job_title:2"] and f["can_skip"] and "Not specified" in f["skip_note"] and f["can_submit"], (list(p), f["can_skip"]))

# ---- what the model refuses ----
code, e = post("harish", {"job_title:1": "  ", "email": "harish at example"})
check("empty required fields and a bad email come back as field errors, nothing created",
      code == 422 and set(e["errors"]) == {"job_title:1", "job_title:2", "email"} and e["errors"]["email"] == "that doesn't look like an email address"
      and e["status"] == "open" and len(fakes.creates) == 1, (code, e.get("errors"), len(fakes.creates)))
code, e = post("harish", {"job_title:1": "Data Engineer", "job_title:2": "Analyst", "phone": "12345"})
check("a phone with too few digits is refused", code == 422 and list(e["errors"]) == ["phone"], e.get("errors"))
code, e = post("harish", {"job_title:1": "A", "job_title:2": "B", "salary": "10 LPA"})
check("a field the form doesn't have is refused", code == 422 and "salary" in e["errors"], e.get("errors"))
code, e = post("harish", {"job_title:1": "x" * 101, "job_title:2": "B"})
check("a value longer than the field allows is refused", code == 422 and "job_title:1" in e["errors"], e.get("errors"))

# ---- submit: corrections applied, profile created, roles matched ----
before = len(fakes.creates)
code, done = post("harish", {"job_title:1": "Data Engineer", "job_title:2": "Software Engineer", "phone": "+91 98765 43210",
                             "address": "Chennai", "skills": "Spark; Airflow", "name": "Harish Ramalingam"})
sent = fakes.creates[-1] if len(fakes.creates) > before else {}
check("a valid form creates the profile and returns the result", code == 200 and done["status"] == "done"
      and done["result"]["profile"]["status"] == "created" and done["result"]["profile"]["candidate_id"].startswith("CAN-STUB"), (code, done))
check("the typed job titles reach Questlight", [j.get("jobTitle") for j in sent.get("workExperience", [])][1:] == ["Data Engineer", "Software Engineer"],
      sent.get("workExperience"))
check("...and so do the corrections: phone, location, added skills", "+91 98765 43210" in json.dumps(sent) and "Chennai" in json.dumps(sent)
      and "Spark" in json.dumps(sent) and "Airflow" in json.dumps(sent), {k: sent.get(k) for k in ("phoneNumber", "address", "skills")})
check("the roles were matched for the new profile", done["result"]["roles_status"] in ("ok", "no_strong_match") and done["result"]["top_roles"], done["result"].get("roles_status"))
code, again = get("harish")
check("afterwards the link shows the result, not a form", code == 200 and again["status"] == "done" and "schema" not in again, again.get("status"))
n = len(fakes.creates)
code, again = post("harish", {"job_title:1": "X", "job_title:2": "Y"})
check("submitting again creates nothing more", code == 200 and again["status"] == "done" and len(fakes.creates) == n, (code, len(fakes.creates)))

# ---- skip ----
code, sk = post("skipme", skip=True)
titles = [j.get("jobTitle") for j in fakes.creates[-1]["workExperience"]]
check("skip creates the profile with 'Not specified' for the missing job titles", code == 200 and sk["status"] == "done"
      and titles[1:] == ["Not specified", "Not specified"] and "a job title was empty" in sk["result"]["profile"]["adjusted"], (code, titles))
code, f = get("noskills")
check("missing skills can be typed in, but not skipped (Questlight needs them)",
      f["schema"]["required"] == ["skills"] and not f["can_skip"] and "skills" in f["skip_note"].lower(), (f["schema"].get("required"), f.get("skip_note")))
n = len(fakes.creates)
code, e = post("noskills", skip=True)
check("...and a skip there is refused with that reason, nothing created", code == 422 and "skills" in e["message"].lower() and len(fakes.creates) == n, (code, e.get("message")))
code, done = post("noskills", {"skills": "Python, FastAPI"})
check("typing the skills creates the profile", code == 200 and done["status"] == "done", (code, done.get("message")))
code, f = get("noedu")
check("education can't be typed in: the form says a new resume is needed, and can't be submitted or skipped",
      f["blocked"] and "education" in f["blocked"][0] and not f["can_submit"] and not f["can_skip"] and "education" not in props(f), f)
code, e = post("noedu", {"name": "Rohit Verma"})
check("...and a submit there is refused", code == 422 and "new version" in e["message"], (code, e.get("message")))

# ---- a resume the parser could read only once its contact details are given ----
code, f = get("incomplete")
check("only the missing contact details are asked, nothing to correct yet, no skip",
      code == 200 and list(props(f)) == ["email", "phone"] and f["schema"]["required"] == ["email", "phone"] and not f["can_skip"]
      and "read again" in f["intro"], (list(props(f)), f.get("intro")))
parses = len(fakes.parse_calls)
code, part = post("incomplete", {"email": "test.person@example.com", "phone": ""})
check("only the email given: the phone is still required, nothing is read yet", code == 422 and list(part["errors"]) == ["phone"]
      and len(fakes.parse_calls) == parses, (code, part.get("errors")))
code, done = post("incomplete", {"email": "test.person@example.com", "phone": "+91 90000 12345"})
check("both given: the resume is read, nothing else is missing, and the profile is created",
      code == 200 and done["status"] == "done" and len(fakes.parse_calls) == parses + 1 and done["result"]["name"] == "Parsed Name", (code, done.get("message")))

# ---- the form follows the file: what the chat already answered isn't asked again ----
fid = r["twice"]["file_id"]


async def answer_in_chat():
    async with ah.session(fakes) as s:
        return await ah.call(s, "provide_missing_details", file_id=fid, answers=[{"id": "job_title:1", "value": "Data Engineer"}])


asyncio.run(answer_in_chat())
code, f = get("twice")
check("a detail answered in chat drops off the form", f["schema"]["required"] == ["job_title:2"], f["schema"].get("required"))

# ---- links that are no longer open ----
resp = web.get("/api/form/not-a-real-token")
check("an unknown or expired link says the form is no longer open (HTTP 410)", resp.status_code == 410 and resp.json()["status"] == "expired"
      and "30 minutes" in resp.json()["message"], resp.text)
intakes.find_by_form(token("twice")).born -= intakes.TTL_S + 1
check("a form older than 30 minutes has expired too", web.get(f"/api/form/{token('twice')}").status_code == 410)

# ---- privacy and the app's password ----
resp = web.get(f"/api/form/{token('skipme')}")
check("the form's answers are never cached, passed on as a Referer, or indexed",
      resp.headers.get("cache-control") == "no-store" and resp.headers.get("referrer-policy") == "no-referrer"
      and "noindex" in resp.headers.get("x-robots-tag", ""), dict(resp.headers))
os.environ["APP_PASSWORD"] = "pw-123"
check("with a password set, the pages still ask for it", web.get("/").status_code == 401 and web.get("/api/log").status_code == 401)
page = web.get(f"/fill/{token('skipme')}")
check("...but a form link opens without it, as a page of its own", page.status_code == 200 and "form-view.js" in page.text
      and page.headers.get("cache-control") == "no-store", page.status_code)
check("...and so do its data and the two files the page loads",
      web.get(f"/api/form/{token('skipme')}").status_code == 200 and web.get("/static/form-view.js").status_code == 200
      and web.get("/static/form.css").status_code == 200, [web.get(u).status_code for u in ("/static/form-view.js", "/static/form.css")])
check("...but not the rest of /static or /api", web.get("/static/trace-view.js").status_code == 401 and web.get("/api/traces").status_code == 401)
os.environ.pop("APP_PASSWORD")

# ---- the model itself ----
m = forms.model([{"id": "email", "label": "Email", "hint": "", "input": "email", "max": 254, "group": "missing", "value": ""}])
check("the form model is a real Pydantic model, with the id as the field's name in the schema",
      list(m.model_json_schema()["properties"]) == ["email"] and m.model_validate({"email": " a@b.co "}).model_dump(by_alias=True) == {"email": "a@b.co"})
finish()
