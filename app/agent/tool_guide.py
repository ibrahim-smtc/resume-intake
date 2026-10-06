"""The "/tools" guide for recruiters: what the assistant can do, in plain words.

Typing /tools in the page shows this (see /api/tools in app/main.py) without calling the agent. EVERY tool in tools.py must
appear in "tools" of some entry: uncovered_tools() lists any that doesn't, and a startup warning plus a test catch a new
tool that was added without a line here.
"""
from typing import TypedDict

from app.agent.mcp_app import mcp

# Spelled out so the editor's type checker knows what an entry holds. "try" is a Python keyword, hence the function syntax.
GuideItem = TypedDict("GuideItem", {"title": str, "tools": list[str], "writes": bool, "does": str, "try": str})


class GuideGroup(TypedDict):
    group: str
    items: list[GuideItem]


TOOL_GUIDE: list[GuideGroup] = [
    {"group": "Taking in resumes", "items": [
        {"title": "Take in a resume", "tools": ["process_resume"], "writes": True,
         "does": "Attach a PDF or DOCX with the + button. I screen out files that aren't resumes, read it, create the "
                 "candidate's profile in Questlight and show the best matching open roles.",
         "try": "Attach the file and press Enter"},
        {"title": "Fill in missing details", "tools": ["provide_missing_details", "create_profile"], "writes": True,
         "does": "If a resume lacks something Questlight requires (a job title, say), I tell you what is missing and ask. "
                 "Answer in the chat and I complete the profile. Say \"go ahead without it\" to save it as \"Not specified\".",
         "try": "Update Candidate DOB: 13/06/2003"},
    ]},
    {"group": "Open roles and activity", "items": [
        {"title": "Open roles", "tools": ["list_open_roles"], "writes": False,
         "does": "How many roles are open in Questlight right now, and which ones. You can search by title.",
         "try": "How many roles are open?  /  Any Python roles?"},
        {"title": "What came in", "tools": ["get_intake_summary", "list_recent_intakes"], "writes": False,
         "does": "Counts for today, this week or any period up to 90 days (accepted, junk, profiles created), and the latest "
                 "resumes with what happened to each.",
         "try": "What came in this week?  /  Show the last 10 resumes"},
    ]},
    {"group": "One step at a time (advanced)", "items": [
        {"title": "Check if a file is a resume", "tools": ["check_junk"], "writes": False,
         "does": "Screening only: tells you if a file is a resume, junk, or needs a person to look. Nothing is created.",
         "try": "Attach a file and ask: is this a resume?"},
        {"title": "Read a resume without creating a profile", "tools": ["parse_resume"], "writes": False,
         "does": "Reads name, contact, experience and skills, and shows what is missing. Nothing is created.",
         "try": "Attach a file and say: just read it, don't create a profile"},
        {"title": "Match a candidate to open roles", "tools": ["match_roles"], "writes": False,
         "does": "Ranks the best 3 open roles for a candidate who already has a profile in Questlight.",
         "try": "Match this candidate to open roles"},
    ]},
]


def uncovered_tools() -> list[str]:
    """Registered tools that have no entry in TOOL_GUIDE (should always be empty)."""
    listed = {t for g in TOOL_GUIDE for item in g["items"] for t in item["tools"]}
    return sorted(t.name for t in mcp._tool_manager.list_tools() if t.name not in listed)
