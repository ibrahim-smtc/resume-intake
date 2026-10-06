"""Who can reach what (app/security.py), and the settings that change on Vercel (app/settings.py)."""
import json
import os
import subprocess
import sys

import harness

harness.isolate(MCP_TOKEN="agent-token")
from harness import ROOT, check, finish  # noqa: E402

from fastapi.testclient import TestClient  # noqa: E402

from app import settings  # noqa: E402
from app.main import app  # noqa: E402

TUNNEL = {"Cf-Ray": "abc", "Cf-Connecting-Ip": "1.2.3.4"}   # what Cloudflare adds to every request it forwards
TOOLS_LIST = {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
AGENT = {"Authorization": "Bearer agent-token", "Accept": "application/json"}

with TestClient(app) as client:
    # ---- running locally, no password ----
    check("local: the pages and APIs are open", all(client.get(p).status_code == 200 for p in ("/", "/log", "/trace", "/api/config", "/api/log", "/static/trace-view.js")))
    check("the auto-generated API docs are switched off", all(client.get(p).status_code == 404 for p in ("/docs", "/redoc", "/openapi.json")))
    check("through a Cloudflare tunnel only /mcp is reachable",
          all(client.get(p, headers=TUNNEL).status_code == 404 for p in ("/", "/log", "/api/log", "/api/config", "/static/trace-view.js", "/classic"))
          and client.post("/api/process", headers=TUNNEL, files={"file": ("x.pdf", b"x")}).status_code == 404
          and client.post("/api/chat", headers=TUNNEL, data={"message": "x"}).status_code == 404)
    r = client.post("/mcp", json=TOOLS_LIST, headers={**AGENT, **TUNNEL})
    check("/mcp works through the tunnel with the token", r.status_code == 200 and len(r.json()["result"]["tools"]) == 9, r.text[:120])
    check("/mcp refuses a missing or wrong token", client.post("/mcp", json=TOOLS_LIST).status_code == 401
          and client.post("/mcp", json=TOOLS_LIST, headers={"Authorization": "Bearer nope"}).status_code == 401)

    # ---- APP_PASSWORD set: the way to put it on the internet ----
    os.environ["APP_PASSWORD"] = "s3cret:with:colons"
    r = client.get("/")
    check("with a password, the page asks for one (Basic auth)", r.status_code == 401 and "Basic" in r.headers.get("www-authenticate", ""))
    check("a wrong password is refused", client.get("/", auth=("anyone", "wrong")).status_code == 401 and client.get("/", auth=("anyone", "")).status_code == 401)
    check("the right password works, with any user name, and may contain colons",
          client.get("/", auth=("anyone", "s3cret:with:colons")).status_code == 200 and client.get("/", auth=("", "s3cret:with:colons")).status_code == 200)
    check("APIs, the audit log and the static files need it too",
          all(client.get(p).status_code == 401 for p in ("/api/log", "/api/traces", "/log", "/trace", "/static/trace-view.js", "/api/tools"))
          and client.post("/api/process", files={"file": ("x.pdf", b"x")}).status_code == 401
          and all(client.get(p, auth=("u", "s3cret:with:colons")).status_code == 200 for p in ("/api/log", "/static/trace-view.js", "/api/tools")))
    check("a malformed Authorization header is refused, not a crash", client.get("/", headers={"Authorization": "Basic !!!not-base64"}).status_code == 401
          and client.get("/", headers={"Authorization": "Bearer agent-token"}).status_code == 401)
    r = client.post("/mcp", json=TOOLS_LIST)
    check("/mcp is NOT behind the password: it has its own bearer token", r.status_code == 401 and "bearer" in r.json()["error"], r.text)
    check("/mcp works with the agent token and no password", client.post("/mcp", json=TOOLS_LIST, headers=AGENT).status_code == 200)
    check("a tunnel request with the password is allowed (the password is the protection then)",
          client.get("/", headers=TUNNEL, auth=("u", "s3cret:with:colons")).status_code == 200)
    os.environ.pop("APP_PASSWORD")

    # ---- on Vercel: open unless a password is set (the testing phase) ----
    settings.ON_VERCEL = True
    check("on Vercel with no password, the pages and APIs are open", all(client.get(p).status_code == 200 for p in ("/", "/log", "/api/config", "/api/tools")))
    check("...and the tunnel rule does not apply there (a proxy's headers must not lock the pages)", client.get("/", headers=TUNNEL).status_code == 200)
    check("on Vercel, /mcp still needs its token", client.post("/mcp", json=TOOLS_LIST).status_code == 401 and client.post("/mcp", json=TOOLS_LIST, headers=AGENT).status_code == 200)
    os.environ["APP_PASSWORD"] = "pw"
    check("on Vercel with a password, the page opens with it", client.get("/", auth=("u", "pw")).status_code == 200 and client.get("/").status_code == 401)
    os.environ.pop("APP_PASSWORD")
    settings.ON_VERCEL = False


# ---- settings differ on Vercel (a fresh process, because they are read once at import) ----
def settings_in_new_process(**env):
    out = subprocess.run([sys.executable, "-c",
                          "import json; from app import settings as s; print(json.dumps([s.ON_VERCEL, s.MAX_UPLOAD_MB, s.MAX_UPLOAD_LABEL, str(s.DEFAULT_AUDIT_DB)]))"],
                         cwd=ROOT, capture_output=True, text=True, env={**os.environ, "SKIP_DOTENV": "1", **env})
    return json.loads(out.stdout) if out.returncode == 0 else out.stderr


local = settings_in_new_process(VERCEL="")
vercel = settings_in_new_process(VERCEL="1")
override = settings_in_new_process(VERCEL="1", MAX_UPLOAD_MB="2")
check("locally: 5 MB limit, audit database in data/", local[:3] == [False, 5.0, "5 MB"] and local[3].replace("\\", "/").endswith("data/audit.db"), local)
check("on Vercel: 4 MB limit (a function can't receive more than 4.5 MB) and audit database in /tmp",
      vercel[:3] == [True, 4.0, "4 MB"] and vercel[3].replace("\\", "/").endswith("/tmp/audit.db"), vercel)
check("MAX_UPLOAD_MB overrides either", override[1:3] == [2.0, "2 MB"], override)
finish()
