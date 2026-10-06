"""The page's text box -> /api/chat -> the Perfox agent's Webhook trigger -> (a mock agent calls our real /mcp) -> the reply back.
Real: our app and MCP server. Mocked: Perfox (a local app standing in for the webhook), the parser and Questlight's profile call."""
import asyncio
import os
import re
import time

import harness

harness.isolate()
from harness import RESUMES, check, finish, serve  # noqa: E402

import agent_harness as ah  # noqa: E402
from fixtures.parsed import ROHIT  # noqa: E402

fakes = ah.start({"rohit": ROHIT})
SECRET = "wh-secret-xyz-789"

import httpx  # noqa: E402
from fastapi import FastAPI, Request, Response  # noqa: E402
from fastapi.responses import JSONResponse  # noqa: E402

from app.agent import chat_bridge, downloads, intakes  # noqa: E402
from app.observability import audit  # noqa: E402

# ---------- a mock of Perfox's webhook ----------
mock = FastAPI()
seen = []                    # every webhook request the mock received
state = {"mode": "agent"}    # agent | 401 | 404 | 422 | noreply | slow | badjson | plain


@mock.post("/hooks/wf/intake")
async def hook(request: Request):
    body = await request.json()
    seen.append({"headers": dict(request.headers), "body": body})
    mode = state["mode"]
    if mode == "401" or request.headers.get("x-webhook-secret") != SECRET:
        return JSONResponse({"error": "bad secret"}, 401)
    if mode in ("404", "422"):
        return JSONResponse({"error": mode}, int(mode))
    if mode == "badjson":
        return Response("not json", 200)
    if mode == "noreply":
        return {"received": True, "execution_id": "exec-1", "attachments": []}
    if mode == "slow":
        await asyncio.sleep(5)
        return {"received": True, "execution_id": "exec-2", "response_text": "late"}
    if mode == "plain":
        return {"received": True, "execution_id": "exec-3", "response_text": "Hello! I take in resumes."}
    # "agent": behave like the Perfox agent. If a file link is in the message, call process_resume over MCP and say what came back.
    m = re.search(r"file_url: (\S+)\]", body["message"])
    if not m:
        return {"received": True, "execution_id": "exec-4", "response_text": "No file attached."}
    async with ah.session(fakes) as s:
        r = await ah.call(s, "process_resume", file_url=m.group(1))
    cand, top = r.get("candidate") or {}, (r.get("top_roles") or [{}])[0]
    text = (f"**{cand.get('name')}** was {r['decision']}.\n\n- Profile: {r['profile']['status']} ({r['profile']['candidate_id']})\n"
            f"- Top role: {top.get('title')} ({top.get('score')})")
    return {"received": True, "execution_id": "exec-5", "response_text": text, "attachments": []}


perfox = serve(mock)
page = fakes.server.url   # the page talks to this
client = httpx.Client(timeout=200)


def chat(message="", session="sess1", file=None):
    return client.post(f"{page}/api/chat", data={"message": message, "session_id": session}, files={"file": file} if file else None)


# ---------- not configured ----------
check("config: no agent, and the upload limit is reported", client.get(f"{page}/api/config").json() == {"agent": False, "max_mb": 5.0})
r = chat("hi")
check("chat without a webhook configured -> 503 with a clear message", r.status_code == 503 and "PERFOX_WEBHOOK_URL" in r.json()["detail"], r.text)

os.environ["PERFOX_WEBHOOK_URL"] = f"{perfox.url}/hooks/wf/intake"
os.environ["PERFOX_WEBHOOK_SECRET"] = SECRET
check("config: agent on once the webhook is set", client.get(f"{page}/api/config").json()["agent"] is True)

# ---------- message only ----------
state["mode"] = "plain"
j = chat("hello there", session="../bad id!!").json()
check("message only: the reply comes back", j["ok"] and j["reply"] == "Hello! I take in resumes." and j["execution_id"] == "exec-3", j)
req = seen[-1]
check("the secret is sent in X-Webhook-Secret", req["headers"].get("x-webhook-secret") == SECRET)
check("the body has the cleaned session id, the message and the channel", req["body"] == {"session_id": "badid", "message": "hello there", "channel": "web"}, req["body"])

# ---------- the whole chain: page -> app -> Perfox (mock agent) -> our /mcp -> pipeline ----------
state["mode"] = "agent"
data = (RESUMES / "Rohit_Verma_Python_Backend.pdf").read_bytes()
t0 = time.time()
j = chat("please take this one in", session="sessA", file=("Rohit_Verma.pdf", data, "application/pdf")).json()
req = seen[-1]
link = re.search(r"file_url: (\S+)\]", req["body"]["message"])
check("a resume goes through the whole chain and the agent's reply comes back", j["ok"] and "Rohit Verma" in j["reply"] and "accepted" in j["reply"] and "CAN-STUB-1" in j["reply"], j)
check("the message carries the user's words, the file name and a private link, not the file",
      link and link.group(1).startswith("intake-file://") and "file_name: Rohit_Verma.pdf" in req["body"]["message"]
      and req["body"]["message"].startswith("please take this one in") and data[:20] not in str(req["body"]).encode() and len(str(req["body"])) < 600)
check("Questlight was reached exactly once, through the stub", len(fakes.creates) == 1, len(fakes.creates))
check("the audit log has the run, with the channel 'perfox agent'", any(x["channel"] == "perfox agent" for x in audit.recent(20)))

# ---------- the private link ----------
name, blob = chat_bridge.load_file(link.group(1))
check("the link returns the same file", name == "Rohit_Verma.pdf" and blob == data)
check("...even with punctuation an LLM adds around it", chat_bridge.load_file("(" + link.group(1) + ").") is not None and chat_bridge.load_file("`" + link.group(1) + "`")[0] == name)
got = asyncio.run(downloads.download(link.group(1), ""))
check("the MCP side opens the link with no network", got[0] == data and got[1] == "Rohit_Verma.pdf" and got[2] is None, got[1:])
check("an unknown link gives a clear error", "expired" in asyncio.run(downloads.download("intake-file://" + "0" * 24, ""))[2])
token = link.group(1)[len("intake-file://"):]
chat_bridge._files[token] = (name, blob, chat_bridge._files[token][2] - 31 * 60)
check("an expired link gives a clear error, and is gone", "expired" in asyncio.run(downloads.download(link.group(1), ""))[2] and chat_bridge.load_file(link.group(1)) is None)
for n in range(25):
    chat_bridge.store_file(f"f{n}.pdf", b"x")
check("at most 20 files are held", len(chat_bridge._files) <= chat_bridge.FILE_MAX, len(chat_bridge._files))
check("a made-up token in text is not mistaken for a file reference", intakes.find("hello") is None and chat_bridge.token_of("no token here") is None)

# ---------- bad input never reaches Perfox ----------
before = len(seen)
check("a wrong file type is refused here", chat("here", file=("photo.png", b"hello", "image/png")).json() == {"ok": False, "error": "photo.png: I can only read PDF, DOCX, TXT or DOC files."})
check("an empty file is refused here", "empty" in chat("here", file=("empty.pdf", b"", "application/pdf")).json()["error"])
check("a file over the limit is refused here", "5 MB" in chat("here", file=("big.pdf", b"%PDF" + b"0" * (5 * 1024 * 1024), "application/pdf")).json()["error"])
check("a blank message is refused here", chat("   ").json() == {"ok": False, "error": "nothing to send"})
check("none of those reached Perfox", len(seen) == before)

# ---------- Perfox's error answers, in plain words, never leaking the secret or the URL ----------
state["mode"] = "401"
e = chat("x").json()["error"]
check("401: tells you to check the secret", "secret" in e.lower() and SECRET not in e, e)
state["mode"] = "404"
e = chat("x").json()["error"]
check("404: agent not found or not active", "isn't active" in e and "127.0.0.1" not in e, e)
state["mode"] = "422"
check("422: agent incomplete", "incomplete" in chat("x").json()["error"])
state["mode"] = "noreply"
e = chat("x").json()["error"]
check("200 without reply text: explains the Auth Mode", "Shared Secret" in e and "exec-1" in e, e)
state["mode"] = "badjson"
check("a non-JSON 200 gives a clear error", "isn't JSON" in chat("x").json()["error"])
state["mode"] = "slow"
chat_bridge.TIMEOUT_S = 1
e = chat("x").json()["error"]
check("a timeout gives a clear error", "didn't answer within" in e, e)
chat_bridge.TIMEOUT_S = 150
state["mode"] = "agent"
os.environ["PERFOX_WEBHOOK_SECRET"] = "wrong-secret"
e = chat("x").json()["error"]
check("a wrong secret in the settings is reported without printing it", "secret" in e.lower() and "wrong-secret" not in e, e)
os.environ["PERFOX_WEBHOOK_SECRET"] = SECRET
os.environ["PERFOX_WEBHOOK_URL"] = "http://127.0.0.1:9/hooks/x"
e = chat("x").json()["error"]
check("an unreachable Perfox gives a clear error without the URL", "couldn't reach Perfox" in e and "9/hooks" not in e, e)
check("the page is still served", client.get(f"{page}/").status_code == 200)
finish()
