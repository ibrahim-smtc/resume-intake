"""Background tasks for requests that can take longer than a proxy waits for an answer.

Cloudflare cuts a request that has had no response for 100 s (a 524), and the chat, the upload page and the fill-in form can each
run longer (a JD becomes a job in up to 150 s, the agent may take 280 s). So these routes start the work here and answer at once
with an id; the page then asks for the result every few seconds (GET .../{id}) until it is there.

Everything is in the memory of this process, like the rest of the agent's state (see "Run exactly one replica" in the README): a
restart loses what was running, and the poll then answers 404 so the page can say so. Ids are long and random, and a task can be
tied to an owner (the fill-in form's token), so one form's id can't be read through another's address."""
import asyncio
import secrets
import time
from collections import OrderedDict

KEEP_S = 10 * 60   # how long a finished result waits to be fetched
MAX_TASKS = 100    # at most this many are remembered; the oldest finished one goes first

PENDING = (202, {"status": "pending"})
UNKNOWN = (404, {"ok": False, "status": "error", "error": "this request is no longer known (the server may have restarted): send it again",
                 "message": "this request is no longer known (the server may have restarted): send it again"})

_tasks: "OrderedDict[str, dict]" = OrderedDict()


def _forget() -> None:
    now = time.monotonic()
    for key in [k for k, t in _tasks.items() if t["task"].done() and now - t["done"] > KEEP_S]:
        del _tasks[key]
    for key in [k for k, t in _tasks.items() if t["task"].done()]:
        if len(_tasks) <= MAX_TASKS:
            break
        del _tasks[key]


def start(work, owner: str = "") -> str:
    """Starts `work` (a coroutine returning (HTTP status, body)) in the background. Returns its id."""
    _forget()
    entry = {"owner": owner, "done": 0.0}

    def finished(_task) -> None:
        entry["done"] = time.monotonic()

    entry["task"] = task = asyncio.ensure_future(work)  # the dict keeps the task alive until it is fetched
    task.add_done_callback(finished)
    key = secrets.token_urlsafe(24)
    _tasks[key] = entry
    return key


def poll(key: str, owner: str = "") -> tuple[int, dict]:
    """(HTTP status, body) for a task: 202 while it runs, else what the work returned. Unknown ids, and ids of another owner, are 404."""
    entry = _tasks.get(str(key or ""))
    if not entry or entry["owner"] != owner:
        return UNKNOWN
    task = entry["task"]
    if not task.done():
        return PENDING
    if task.cancelled():
        return 500, {"ok": False, "status": "error", "error": "the request was cancelled", "message": "the request was cancelled"}
    if task.exception() is not None:
        print(f"[tasks] a background request failed: {type(task.exception()).__name__}", flush=True)  # the type only: no details
        return 500, {"ok": False, "status": "error", "error": "the request failed on the server", "message": "the request failed on the server"}
    return task.result()
