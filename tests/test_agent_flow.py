"""The conversation flows the agent runs: a resume that lacks something Questlight requires (two jobs with no title), the recruiter
supplying it, "go ahead without it", a parser that can't read the email, and what happened in the real chat: Perfox keeps only
the chat MESSAGES between turns, so the agent has just the attachment's link and passes it (or its token) where a file_id belongs.
Parser and Questlight are fake; the job list is hand-made."""
import asyncio
import json
import os
import sqlite3

import harness

harness.isolate()
from harness import check, finish  # noqa: E402

import agent_harness as ah  # noqa: E402
from fixtures.jobs import OPEN_JOB_COUNT  # noqa: E402
from fixtures.parsed import HARISH  # noqa: E402

UNREADABLE_EMAIL = "We couldn't find the following details: email"
fakes = ah.start({"harish": HARISH, "fill": HARISH, "incomplete": UNREADABLE_EMAIL})

from app.agent import chat_bridge, intakes  # noqa: E402


async def main():
    o = {}
    async with ah.session(fakes) as s:
        call = lambda tool, **a: ah.call(s, tool, **a)  # noqa: E731
        o["tools"] = [t.name for t in (await s.list_tools()).tools]

        # 1. two untitled jobs: nothing created, and the agent is told exactly what to ask
        o["p1"] = await call("process_resume", file_url="https://files.test/harish.pdf")
        fid = o["p1"].get("file_id")
        o["creates_after_p1"] = len(fakes.creates)
        # 2. bad answers are all rejected
        o["bad"] = await call("provide_missing_details", file_id=fid, answers=[
            {"id": "job_title:9", "value": "X"}, {"id": "nonsense", "value": "X"}, {"id": "job_title:1", "value": "   "},
            {"id": "email", "value": "not-an-email"}])
        o["early_create"] = await call("create_profile", file_id=fid)
        # 3. "he's a Data Engineer": one answer for both untitled jobs
        o["ans"] = await call("provide_missing_details", file_id=fid, answers=[
            {"id": "job_title:1", "value": "Data Engineer"}, {"id": "job_title:2", "value": "Data Engineer"}])
        o["creates_before"] = len(fakes.creates)
        o["c1"] = await call("create_profile", file_id=fid)
        o["c2"] = await call("create_profile", file_id=fid)
        o["creates_after"] = len(fakes.creates)
        o["titles"] = [j["jobTitle"] for j in fakes.creates[-1]["workExperience"]]
        o["late"] = await call("provide_missing_details", file_id=fid, answers=[{"id": "job_title:1", "value": "Y"}])

        # 4. "go ahead without it"
        f2 = (await call("process_resume", file_url="https://files.test/fill.pdf"))["file_id"]
        n = len(fakes.creates)
        o["fill"] = await call("create_profile", file_id=f2, fill_missing=True)
        o["fill_titles"] = [j["jobTitle"] for j in fakes.creates[n]["workExperience"]] if len(fakes.creates) > n else None

        # 5. the parser can't read the email at all
        o["i1"] = await call("process_resume", file_url="https://files.test/incomplete.pdf")
        i_fid = o["i1"].get("file_id")
        o["i2"] = await call("provide_missing_details", file_id=i_fid, answers=[
            {"id": "name", "value": "Test Person"}, {"id": "email", "value": "test.person@example.com"}])
        o["i3"] = await call("create_profile", file_id=i_fid)

        # 6. the real chat: the agent has only the attachment's link
        link = chat_bridge.store_file("harish.pdf", (harness.RESUMES / "Rohit_Verma_Python_Backend.pdf").read_bytes())
        token = link[len("intake-file://"):]
        o["l1"] = await call("process_resume", file_url=link, file_name="harish.pdf")
        before = len(fakes.parse_calls)
        o["l2"] = await call("process_resume", file_url=link, file_name="harish.pdf")          # the agent calls it again
        o["l2_parses"] = len(fakes.parse_calls) - before
        n = len(fakes.creates)
        o["l3"] = await call("provide_missing_details", file_id=token, answers=[                # the TOKEN as the file_id
            {"id": "job_title:1", "value": "Jr. Data Engineer"}, {"id": "job_title:2", "value": "Jr. Data Engineer"}])
        o["l4"] = await call("create_profile", file_id=f"({link}).")                            # the link, with punctuation
        o["l4_creates"] = len(fakes.creates) - n
        o["l4_titles"] = [j["jobTitle"] for j in fakes.creates[-1]["workExperience"]]
        o["l5"] = await call("match_roles", file_id=token)
        o["l6"] = await call("process_resume", file_url=link)                                  # asked again after creation
        b = len(fakes.parse_calls)
        o["l7"] = await call("parse_resume", file_id=token)
        o["l7_parses"] = len(fakes.parse_calls) - b
        o["l8"] = await call("match_roles", file_id="deadbeef")

        # 7. open roles
        o["roles"] = await call("list_open_roles", limit=3)
        o["roles_java"] = await call("list_open_roles", search="java", limit=100)
        o["roles_none"] = await call("list_open_roles", search="zzzqqq-no-such-title")
        o["roles_clamped"] = await call("list_open_roles", limit=1000)
    return o


o = asyncio.run(main())

p = o["p1"]
check("untitled jobs: accepted, the profile is NOT created and nothing is sent to Questlight",
      p["decision"] == "accepted" and p["profile_created"] is False and p["profile"]["status"] == "not_created" and o["creates_after_p1"] == 0, p)
check("missing_items name the two jobs, with ids the recruiter's answer can use",
      [i["id"] for i in p["missing_items"]] == ["job_title:1", "job_title:2"] and "Walmart Global Tech India" in p["missing_items"][0]["for"]
      and "Tata Consultancy Services" in p["missing_items"][1]["for"] and all(i["can_supply"] for i in p["missing_items"]), p["missing_items"])
check("info_complete is false and missing_required is readable", p["info_complete"] is False and p["missing_required"][0] == "a job title for the job at Walmart Global Tech India")
check("the agent gets a file_id and is told what to do next", p.get("file_id") and "provide_missing_details" in p["next"], p.get("next"))
check("bad answers are all rejected (bad index, unknown id, empty value, bad email)", o["bad"]["applied"] == [] and len(o["bad"]["rejected"]) == 4, o["bad"])
check("create_profile before the answers: refused, still nothing sent", o["early_create"]["profile"]["status"] == "not_created"
      and o["early_create"]["info_complete"] is False and o["creates_before"] == 0)
check("the recruiter's answers are applied, and nothing is written to Questlight by supplying them",
      o["ans"]["applied"] == ["job_title:1", "job_title:2"] and o["ans"]["rejected"] == [] and o["ans"]["info_complete"] is True and o["creates_before"] == 0, o["ans"])
check("create_profile sends the recruiter's titles, not 'Not specified'", o["titles"] == ["Senior Data Engineer", "Data Engineer", "Data Engineer"], o["titles"])
check("the profile's notes say it came in through the agent", "Source: resume intake, perfox agent" in fakes.creates[0]["additionalNotes"], fakes.creates[0]["additionalNotes"])
check("the profile is created with nothing adjusted", o["c1"]["profile_created"] is True and o["c1"]["profile"]["adjusted"] == [] and o["c1"]["profile"]["candidate_id"], o["c1"])
check("creating again returns the saved result and does NOT reach Questlight again", o["c2"]["profile"] == o["c1"]["profile"] and o["creates_after"] == 1, o["creates_after"])
check("once created, answers are refused (no editing a created profile)", o["late"]["ok"] is False and "already exists" in o["late"]["error"], o["late"])

check("'go ahead without it': created, with 'Not specified' titles, and the agent is told what was filled in",
      o["fill"]["profile_created"] is True and o["fill_titles"][1:] == ["Not specified", "Not specified"] and "a job title was empty" in o["fill"]["profile"]["adjusted"], o["fill"])

check("parser can't read the email: the file is kept and only the email is asked for", o["i1"]["profile"]["status"] == "not_created" and o["i1"].get("file_id")
      and [i["id"] for i in o["i1"]["missing_items"]] == ["email"], o["i1"])
check("name and email can be supplied, but skills, work history and education can't be invented",
      o["i2"]["applied"] == ["name", "email"] and o["i2"]["info_complete"] is False
      and {i["id"]: i["can_supply"] for i in o["i2"]["missing_items"]} == {"skills": True, "workExperience": False, "education": False}, o["i2"])
check("...so create_profile still refuses and sends nothing", o["i3"]["profile"]["status"] == "not_created" and not any(c.get("email") == "test.person@example.com" for c in fakes.creates))

check("chat link: the first process_resume asks for the two job titles", o["l1"]["profile"]["status"] == "not_created" and len(o["l1"]["missing_items"]) == 2 and o["l1"].get("file_id"))
check("calling process_resume again for the same attachment does NOT run the parser again",
      o["l2_parses"] == 0 and o["l2"]["file_id"] == o["l1"]["file_id"] and "already processed" in o["l2"]["reason"] and len(o["l2"]["missing_items"]) == 2, (o["l2_parses"], o["l2"].get("reason")))
check("the link's TOKEN works where a file_id belongs", o["l3"]["ok"] and o["l3"]["applied"] == ["job_title:1", "job_title:2"] and o["l3"]["info_complete"], o["l3"])
check("the whole link, even with punctuation around it, works too", o["l4"]["profile_created"] is True and o["l4_creates"] == 1, o["l4"])
check("the recruiter's title reached Questlight", o["l4_titles"] == ["Senior Data Engineer", "Jr. Data Engineer", "Jr. Data Engineer"], o["l4_titles"])
check("a later turn: match_roles by token returns the roles", o["l5"]["ok"] and o["l5"]["roles_status"] in ("ok", "no_strong_match") and len(o["l5"]["top_roles"]) >= 1, o["l5"])
check("a later turn: process_resume shows the created profile and the roles, and writes nothing",
      o["l6"]["profile_created"] is True and o["l6"]["missing_items"] == [] and len(o["l6"]["top_roles"]) >= 1 and "next" not in o["l6"] and o["l4_creates"] == 1, o["l6"])
check("parse_resume by token returns the saved data and does NOT parse again", o["l7"]["ok"] and o["l7"]["info_complete"] and o["l7_parses"] == 0, (o["l7"].get("info_complete"), o["l7_parses"]))
check("a nonsense reference says what to pass instead", o["l8"]["ok"] is False and "file_url" in o["l8"]["error"], o["l8"])

r = o["roles"]
check("open roles: the real count and a short sample", r["ok"] and r["open_roles"] == OPEN_JOB_COUNT and r["all_open_roles"] == OPEN_JOB_COUNT and len(r["shown"]) == 3
      and all(x["title"] and x["job_id"] for x in r["shown"]), r)
check("open roles: a title search narrows the count", o["roles_java"]["open_roles"] == 1 and o["roles_java"]["shown"][0]["job_id"] == "JOB-J1", o["roles_java"])
check("open roles: no match gives zero and an empty list", o["roles_none"]["open_roles"] == 0 and o["roles_none"]["shown"] == [])
check("open roles: the limit is capped", len(o["roles_clamped"]["shown"]) <= 30)

# the audit log and traces hold no names, companies or values the recruiter typed
conn = sqlite3.connect(os.environ["AUDIT_DB"])
dump = json.dumps(conn.execute("SELECT * FROM audit_log").fetchall()) + json.dumps(conn.execute("SELECT details, error FROM trace_spans").fetchall())
steps = [x[0] for x in conn.execute("SELECT step FROM audit_log WHERE run_id = ? ORDER BY id", (o["p1"]["file_id"],))]
conn.close()
check("audit: the supplied details are logged by field id only, between parse and load", steps[:3] == ["junk_check", "parse", "load_questlight"] and "correction" in steps, steps)
check("audit and traces hold no names, companies or values the recruiter typed",
      not any(w in dump for w in ("Walmart", "Tata", "Data Engineer", "Harish", "LatentView", "Test Person")))
check("the saved files are kept per process, and at most 20 at once", len(intakes._intakes) <= intakes.MAX_FILES)
finish()
