"""The agent's working memory: the files it has taken in and where each one stands.

The file, the parsed resume and every result stay here on the server; the agent only passes a short reference around, so
candidate data never has to travel through the model. Perfox keeps only the chat MESSAGES between turns, not tool
results, so a reference can be three things: the short file_id, the attachment's intake-file link, or just that link's
token. All of them find the same file.

Memory is per process: a file is kept 30 minutes and at most 20 are held (the oldest goes first). On a host that starts
several instances (Vercel), a later call may land on another one; see README.md, "Deploying to Vercel".
"""
import time
from collections import OrderedDict
from pathlib import Path

from app.agent import chat_bridge
from app.observability import audit

TTL_S = 30 * 60
MAX_FILES = 20

_intakes: "OrderedDict[str, Intake]" = OrderedDict()
_by_upload: dict = {}  # upload token (from the intake-file link) -> intake id


class Intake:
    """One file the agent is working on."""

    def __init__(self, run: audit.Run, name: str, ext: str, data: bytes, text: str):
        self.id = run.run_id
        self.run, self.name, self.ext, self.data, self.text = run, name, ext, data, text
        self.born = time.monotonic()
        self.parsed = self.incomplete = self.profile = self.roles = None
        self.parse_done = False
        self.upload = None  # the token of the intake-file link this file came in with (chat uploads), if any


def remember(intake: Intake) -> None:
    now = time.monotonic()
    for key in [k for k, v in _intakes.items() if now - v.born > TTL_S]:
        del _intakes[key]
    _intakes[intake.id] = intake
    while len(_intakes) > MAX_FILES:
        _intakes.popitem(last=False)
    if intake.upload:
        _by_upload[intake.upload] = intake.id
    for token in [t for t, i in _by_upload.items() if i not in _intakes]:  # forget links to files that are gone
        del _by_upload[token]


def find(ref):
    """The live intake for a reference (file_id, intake-file link, or its token), else None."""
    ref = str(ref or "").strip()
    intake = _intakes.get(ref)
    if not intake:
        token = chat_bridge.token_of(ref)
        intake = _intakes.get(_by_upload.get(token)) if token else None
    return intake if intake and time.monotonic() - intake.born <= TTL_S else None


def lookup(ref):
    """Returns (intake, error). A reference that matches nothing gets a message the agent can act on."""
    intake = find(ref)
    if intake:
        return intake, None
    return None, ("no file found for that reference. file_id is the one in the process_resume result; if you no longer "
                  "have it, pass the attachment's file_url instead. Files are kept for 30 minutes: if it has expired, call "
                  "process_resume again with the attachment's file_url")


def keep_for_follow_up(state: dict, name: str, data: bytes, result: dict, upload=None):
    """Keeps a parsed resume (whatever happened to its profile) so that the next tool calls, even in a later turn of the
    chat, can continue with the same file: supply a missing detail, retry, match roles. Junk and unreadable files have
    nothing to follow up. `state` is what pipeline.process_file filled in."""
    status = (result.get("profile") or {}).get("status")
    if "run" not in state or "parsed" not in state or not status:
        return None
    intake = Intake(state["run"], name, Path(name).suffix.lower(), data, state.get("text", ""))
    intake.parse_done, intake.parsed, intake.incomplete, intake.profile = True, state["parsed"], state.get("incomplete"), result["profile"]
    intake.roles, intake.upload = result.get("roles"), upload
    remember(intake)
    return intake
