"""The agent's tools over real HTTP, with the real MCP client: the one-call process_resume, the step tools and the rules they
enforce, retries, duplicates, expiry, downloads, and what lands in the audit log and traces. The parser and Questlight are fake."""
import asyncio
import json
import os
import sqlite3

import harness

harness.isolate()
from harness import check, finish  # noqa: E402

import agent_harness as ah  # noqa: E402
from fixtures.parsed import ROHIT  # noqa: E402

fakes = ah.start({"rohit": ROHIT, "ananya": ROHIT, "again": ROHIT, "retry": ROHIT, "dup": ROHIT, "fill": ROHIT})

import httpx  # noqa: E402

from app.agent import downloads, intakes  # noqa: E402
from app.observability import tracing  # noqa: E402

PDF = b"%PDF-1.4 minimal"


# ---- downloading the file the agent points at (no network: httpx is given a canned answer) ----
REAL_CLIENT = httpx.AsyncClient


def with_remote(handler):
    downloads.httpx.AsyncClient = lambda **kw: REAL_CLIENT(transport=httpx.MockTransport(handler), **kw)  # type: ignore[assignment]


async def fetch(url, name=""):
    return await downloads.download(url, name)


def remote(status=200, body=PDF):
    with_remote(lambda request: httpx.Response(status, content=body))


check("a file name without an extension gets one from the file's content",
      downloads.file_name_for("abc123", b"%PDF-1.4 x") == "abc123.pdf" and downloads.file_name_for("x", b"PK\x03\x04 word/document.xml") == "x.docx"
      and downloads.file_name_for("x.pdf", b"anything") == "x.pdf" and downloads.file_name_for("x", b"hello") == "x")
check("only https links are fetched", asyncio.run(fetch("http://a.com/x.pdf"))[2] == "file_url must be an https link to the uploaded file"
      and "https" in asyncio.run(fetch("file:///C:/Windows/win.ini"))[2])
os.environ["MCP_FILE_HOSTS"] = "perfox.ai"
check("MCP_FILE_HOSTS limits where files may come from", "not accepted" in asyncio.run(fetch("https://example.com/a.pdf"))[2])
remote()
check("...and allows its subdomains", asyncio.run(fetch("https://files.perfox.ai/a.pdf"))[2] is None)
os.environ.pop("MCP_FILE_HOSTS")
remote(404)
check("a dead link gives a clear error", "HTTP 404" in asyncio.run(fetch("https://example.com/gone.pdf"))[2])
remote(200, b"%PDF" + b"0" * (5 * 1024 * 1024 + 10))
check("a file over the size limit is refused while downloading", "larger than 5 MB" in asyncio.run(fetch("https://example.com/big.pdf"))[2])
remote(200, PDF)
data, name, err = asyncio.run(fetch("https://example.com/files/My%20Resume%20(1)", ""))
check("a good link returns the bytes, and a name taken from the URL with its extension added", err is None and data == PDF and name == "My Resume (1).pdf", (name, err))
downloads.httpx.AsyncClient = REAL_CLIENT  # restore


async def main():
    out = {}
    async with ah.session(fakes) as s:
        call = lambda tool, **a: ah.call(s, tool, **a)  # noqa: E731
        out["tools"] = {t.name: t for t in (await s.list_tools()).tools}

        # ---- process_resume: the whole intake in one call ----
        out["rohit"] = await call("process_resume", file_url="https://files.test/rohit.pdf", file_name="Rohit_Verma.pdf")
        out["junk"] = await call("process_resume", file_url="https://files.test/invoice")
        out["bad_url"] = await call("process_resume", file_url="http://insecure.test/x.pdf")
        out["summary"] = await call("get_intake_summary", days=1)
        out["recent"] = await call("list_recent_intakes", limit=5)

        # ---- the step tools, in order, with the rules enforced whatever the order ----
        junk = await call("check_junk", file_url="https://files.test/ananya.pdf", file_name="Ananya.pdf")
        fid = junk.get("file_id")
        out["step_junk"] = junk
        n = len(fakes.creates)
        out["early_create"] = await call("create_profile", file_id=fid)
        out["early_match"] = await call("match_roles", file_id=fid)
        out["parse"] = await call("parse_resume", file_id=fid)
        out["parse2"] = await call("parse_resume", file_id=fid)
        out["match_no_profile"] = await call("match_roles", file_id=fid)
        out["create"] = await call("create_profile", file_id=fid)
        out["create2"] = await call("create_profile", file_id=fid)
        out["creates_here"] = len(fakes.creates) - n
        out["late_provide"] = await call("provide_missing_details", file_id=fid, answers=[{"id": "job_title:0", "value": "x"}])
        out["match"] = await call("match_roles", file_id=fid)
        out["fid"] = fid
        out["junk_step"] = await call("check_junk", file_url="https://files.test/invoice")
        out["bogus"] = [await call("parse_resume", file_id="deadbeef0000"), await call("create_profile", file_id="deadbeef0000"),
                        await call("match_roles", file_id="deadbeef0000"),
                        await call("provide_missing_details", file_id="deadbeef0000", answers=[])]

        # ---- a duplicate can still be matched ----
        fakes.create_mode = "duplicate"
        d = (await call("check_junk", file_url="https://files.test/dup.pdf"))["file_id"]
        await call("parse_resume", file_id=d)
        out["dup_create"] = await call("create_profile", file_id=d)
        out["dup_match"] = await call("match_roles", file_id=d)

        # ---- a failed creation is retried; fill_missing is accepted when nothing is missing ----
        fakes.create_mode = "fail_once"
        r1 = await call("process_resume", file_url="https://files.test/retry.pdf")
        out["retry1"] = r1
        out["retry2"] = await call("create_profile", file_id=r1.get("file_id"))

        # ---- expiry ----
        intakes._intakes[fid].born -= 31 * 60  # type: ignore[index]
        out["expired"] = await call("parse_resume", file_id=fid)
        out["parse_calls_total"] = len(fakes.parse_calls)
    return out


o = asyncio.run(main())
tools = o["tools"]
check("the agent sees the 9 tools", set(tools) == {"process_resume", "check_junk", "parse_resume", "provide_missing_details", "create_profile",
                                                   "match_roles", "list_open_roles", "get_intake_summary", "list_recent_intakes"}, sorted(tools))
check("process_resume's schema needs a file_url", tools["process_resume"].inputSchema.get("required") == ["file_url"])
check("tool descriptions don't promise a fixed file size", "5 MB" not in tools["process_resume"].description and "5 MB" not in tools["check_junk"].description)

r = o["rohit"]
check("a resume is accepted, parsed with the configured parser and its profile created",
      r["ok"] and r["decision"] == "accepted" and r["parser"] == "questlight" and r["profile_created"] and r["profile"]["candidate_id"].startswith("CAN-STUB"), r)
check("the agent gets a short candidate summary", r["candidate"]["name"] == "Rohit Verma" and r["candidate"]["email"] == "rohit.verma.test1@example.com"
      and r["candidate"]["experience"] == "7 years 4 months" and "Python" in r["candidate"]["skills"])
check("...and not the full parsed JSON", "workExperience" not in json.dumps(r) and "redacted" not in r)
check("info is complete, and the top roles carry scores", r["info_complete"] is True and r["missing_required"] == [] and 1 <= len(r["top_roles"]) <= 3
      and all("score" in m for m in r["top_roles"]), r["top_roles"])
check("a created profile keeps a handle, with nothing left to do", r.get("file_id") and "next" not in r)
check("the invoice (a name without an extension) is recognised as a PDF and judged junk, with no handle",
      o["junk"]["decision"] == "junk" and o["junk"]["file"] == "invoice.pdf" and o["junk"]["profile_created"] is False and "file_id" not in o["junk"], o["junk"])
check("a bad link gives decision 'error' with the reason", o["bad_url"]["decision"] == "error" and "https" in o["bad_url"]["error"])
s = o["summary"]
check("the summary counts what came in (2 files: 1 accepted, 1 junk, 1 profile created)", s["files_received"] == 2 and s["screening"] == {"accepted": 1, "junk": 1} and s["profiles"] == {"created": 1}, s)
rec = o["recent"]["intakes"]
check("recent intakes: newest first, with the channel 'perfox agent'", len(rec) == 2 and rec[0]["file"] == "invoice.pdf" and rec[1]["candidate_id"].startswith("CAN-STUB")
      and all(x["channel"] == "perfox agent" for x in rec), rec)

fid = o["fid"]
check("check_junk accepts a resume and hands out a file_id", o["step_junk"]["decision"] == "accepted" and fid)
check("create_profile before parsing is refused, and so is matching", o["early_create"]["ok"] is False and "parse_resume first" in o["early_create"]["error"]
      and o["early_match"]["ok"] is False and "no Questlight profile" in o["early_match"]["error"])
check("parse_resume reads the candidate", o["parse"]["ok"] and o["parse"]["candidate"]["name"] == "Rohit Verma" and o["parse"]["info_complete"])
check("...and a second call returns the saved result without parsing again", o["parse2"]["candidate"] == o["parse"]["candidate"]
      and fakes.parse_calls.count("Ananya.pdf") == 1, fakes.parse_calls)
check("matching before a profile exists is refused", o["match_no_profile"]["ok"] is False)
check("create_profile creates once; repeating it returns the saved result and does not reach Questlight again",
      o["create"]["profile_created"] and o["create2"]["profile"] == o["create"]["profile"] and o["creates_here"] == 1, o["creates_here"])
check("answers can't be added once the profile exists", o["late_provide"]["ok"] is False and "already exists" in o["late_provide"]["error"])
check("match_roles then returns scored roles", o["match"]["ok"] and o["match"]["roles_status"] in ("ok", "no_strong_match") and len(o["match"]["top_roles"]) >= 1)
check("a junk file gets no handle", "file_id" not in o["junk_step"] and o["junk_step"]["decision"] == "junk")
check("made-up references are refused by every step tool, saying what to pass instead",
      all(b["ok"] is False and "no file found" in b["error"] and "file_url" in b["error"] for b in o["bogus"]), o["bogus"])
check("a duplicate profile is reported, nothing changed, and can still be matched", o["dup_create"]["profile"]["status"] == "duplicate" and o["dup_create"]["profile_created"] is False
      and o["dup_match"]["ok"] and len(o["dup_match"]["top_roles"]) >= 1)
check("a failed creation: process_resume keeps the file and offers a retry", o["retry1"]["profile"]["status"] == "failed" and o["retry1"].get("file_id") and "retries" in o["retry1"]["next"], o["retry1"])
check("...and create_profile retries and succeeds", o["retry2"]["profile_created"] is True, o["retry2"])
check("an expired file_id is refused", o["expired"]["ok"] is False and "expired" in o["expired"]["error"])

# ---- the audit log and the traces ----
conn = sqlite3.connect(os.environ["AUDIT_DB"])
steps = [x[0] for x in conn.execute("SELECT step FROM audit_log WHERE run_id = ? ORDER BY id", (fid,))]
conn.close()
check("one run id for the step-by-step file: junk check, parse, load into Questlight, matching", steps == ["junk_check", "parse", "load_questlight", "match_roles"], steps)
trace = tracing.get_trace(f"{fid}-parse_resume")
check("each tool call has its own trace, id = run id + tool, readable with the file name", trace and trace["file"] == "Ananya.pdf" and "parse resume" in [x["name"] for x in trace["spans"]])
check("recent traces list the step traces with the file name", any(x["trace_id"] == f"{fid}-check_junk" and x["file"] == "Ananya.pdf" for x in tracing.recent_traces(100)))
finish()
