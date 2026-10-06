"""The "/tools" guide: every real tool has a plain-words entry, no entry names a tool that doesn't exist, writes are flagged, the
guide is served at /api/tools, and the page really has the /tools command."""
import os
import re

import harness

harness.isolate()
from harness import ROOT, check, finish  # noqa: E402

from fastapi.testclient import TestClient  # noqa: E402

from app.agent import tool_guide  # noqa: E402
from app.agent.mcp_app import mcp  # noqa: E402
from app.main import app  # noqa: E402

import importlib  # noqa: E402

importlib.import_module("app.agent.tools")   # registers the tools (app.main does this when it attaches /mcp)
real = sorted(t.name for t in mcp._tool_manager.list_tools())
listed = sorted({t for g in tool_guide.TOOL_GUIDE for i in g["items"] for t in i["tools"]})

check("every real tool has a /tools entry", tool_guide.uncovered_tools() == [], tool_guide.uncovered_tools())
check("no entry names a tool that doesn't exist (typo guard)", set(listed) <= set(real), sorted(set(listed) - set(real)))
check(f"all {len(real)} tools are listed", listed == real, (real, listed))
check("every entry has a title, a plain description, an example and its tools",
      all(i["title"] and i["does"] and i["try"] and i["tools"] for g in tool_guide.TOOL_GUIDE for i in g["items"]))
writers = {t for g in tool_guide.TOOL_GUIDE for i in g["items"] if i["writes"] for t in i["tools"]}
check("the entries that write to Questlight are flagged", {"process_resume", "create_profile", "match_roles"} <= writers, writers)
check("read-only tools are NOT flagged as writing",
      not ({"list_open_roles", "get_intake_summary", "check_junk", "parse_resume", "list_recent_intakes"} & writers), writers)

client = TestClient(app)
j = client.get("/api/tools").json()
check("/api/tools serves the guide", j["groups"] == tool_guide.TOOL_GUIDE and j["agent"] is False)
os.environ["PERFOX_WEBHOOK_URL"], os.environ["PERFOX_WEBHOOK_SECRET"] = "http://x", "y"
check("...and says whether the agent is connected", client.get("/api/tools").json()["agent"] is True)

page = (ROOT / "app" / "static" / "index.html").read_text(encoding="utf-8")
check("the page answers /tools and /help itself, without sending them to the agent",
      re.search(r"/\^\\/\(tools\|help\)\$/i", page) is not None and "showTools" in page and 'fetch("/api/tools")' in page)
check("the page takes the upload limit from /api/config instead of hard-coding it", "max_mb" in page and "5 MB" not in page.replace("up to ${", ""), "hard-coded 5 MB left in the page")
finish()
