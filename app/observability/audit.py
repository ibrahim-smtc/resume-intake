"""Append-only MongoDB audit log storing upload decisions without candidate PII.

MongoDB also holds the step traces (tracing.py) and the Perfox token usage. All of it is configured by MONGODB_URI (see
settings.py), so a bigger deployment (replica set, Atlas, credentials, TLS, pool size, timeouts) is a change of that string."""
import hashlib
import json
import os
import threading
import time
import uuid
from datetime import datetime, timezone

from bson.errors import BSONError
from pymongo import ASCENDING, MongoClient
from pymongo.errors import ConfigurationError, ConnectionFailure, PyMongoError

from app import settings

CHANNEL = "manual upload"

# What a failed write can raise: the server is unreachable or rejects it, the document can't be encoded, or a setting is wrong.
STORE_ERRORS = (PyMongoError, BSONError, OSError)

RETRY_AFTER_S = 60
DEFAULT_TIMEOUTS_MS = {"serverSelectionTimeoutMS": 5000, "connectTimeoutMS": 5000}

_lock = threading.Lock()
_clients: dict = {}   # one pooled, thread-safe MongoClient per connection string
_ready: set = set()   # (connection string, database) pairs whose collections and indexes exist
_down: dict = {}      # connection string -> time.monotonic() before which it is not tried again


def _uri() -> str:
    return os.getenv("MONGODB_URI") or settings.MONGODB_URI


def _timeouts(uri: str) -> dict:
    lowered = uri.lower()
    return {name: ms for name, ms in DEFAULT_TIMEOUTS_MS.items() if name.lower() not in lowered}


def store_failed(exc: Exception) -> None:
    uri, now = _uri(), time.monotonic()
    if isinstance(exc, ConnectionFailure) and _down.get(uri, 0) <= now:
        _down[uri] = now + RETRY_AFTER_S


def short(exc: Exception) -> str:
    """An error for a log line: cut short, and never the connection string (it can hold a password)."""
    return f"{type(exc).__name__}: {str(exc)[:200]}"


def db():
    """The database, with its collections and indexes created on first use. MONGODB_URI and MONGODB_DB are read on each call, so
    a test or a deployment can switch them. Raises when MongoDB can't be reached: callers that must not fail catch STORE_ERRORS."""
    uri = _uri()
    if _down.get(uri, 0) > time.monotonic():
        raise ConnectionFailure("MongoDB could not be reached a moment ago, so it is not tried again yet")
    name = (os.getenv("MONGODB_DB") or settings.MONGODB_DB).strip()
    with _lock:
        client = _clients.get(uri)
        if client is None:
            try:
                client = _clients[uri] = MongoClient(uri, appname="questlight-resume-intake", **_timeouts(uri))  # connects lazily, on the first operation
            except ValueError:  # some malformed strings (a bad port, say) come out of the driver as a plain ValueError
                raise ConfigurationError("MONGODB_URI is not a valid connection string") from None  # its text may hold a password
    database = client[name] if name else client.get_default_database(default=settings.DEFAULT_MONGODB_DB)
    key = (uri, database.name)
    if key not in _ready:
        try:
            _create_indexes(database)
        except ConnectionFailure as exc:
            store_failed(exc)
            raise
        _ready.add(key)  # only after it worked, so a MongoDB that was down is tried again
    return database


def _create_indexes(database) -> None:
    database[settings.AUDIT_COLLECTION].create_index("run_id")
    database[settings.AUDIT_COLLECTION].create_index("ts")
    database[settings.TRACE_COLLECTION].create_index([("trace_id", ASCENDING), ("seq", ASCENDING)])
    database[settings.TRACE_COLLECTION].create_index("parent_id")
    database[settings.USAGE_EVENTS_COLLECTION].create_index([("trace_id", ASCENDING), ("span_id", ASCENDING)], unique=True)
    database[settings.USAGE_EVENTS_COLLECTION].create_index("ts")
    database[settings.USAGE_DAILY_COLLECTION].create_index([("day", ASCENDING), ("provider", ASCENDING), ("model", ASCENDING)], unique=True)


def ensure_schema() -> None:
    """Creates the collections and indexes now (at startup), so they exist before the first upload."""
    db()


def plain(value):
    """A copy of a dict of facts that MongoDB can store: JSON types only (anything else becomes its text), None when empty."""
    return json.loads(json.dumps(value, default=str)) if value else None


class Run:
    """One uploaded file going through the pipeline. Call .log() at each step."""

    def __init__(self, file: str, data: bytes, channel: str = CHANNEL):
        self.run_id = uuid.uuid4().hex[:12]
        self.file = file
        self.file_hash = hashlib.sha256(data).hexdigest() if data else None
        self.channel = channel

    def log(self, step: str, decision: str, reason: str = "", record_id: str | None = None, **details) -> None:
        # A broken log must not stop intake, but it must be loud: a silent gap would make the reports wrong.
        try:
            db()[settings.AUDIT_COLLECTION].insert_one({
                "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),  # UTC, ISO 8601
                "run_id": self.run_id,      # ties together the steps of one upload
                "channel": self.channel,
                "file": self.file,
                "file_hash": self.file_hash,  # sha256 of the file
                "step": step,               # intake | junk_check | parse | load_questlight ...
                "decision": decision,
                "reason": reason,
                "record_id": record_id,     # Questlight applicant ID (CAN-...), once there is one
                "details": plain(details)})  # small facts: rule signals, missing field names. Never candidate data
        except STORE_ERRORS as exc:
            store_failed(exc)
            print(f"[audit] WARNING: couldn't write to the audit log ({short(exc)})")


def _row(doc: dict) -> dict:
    row = dict(doc)
    row["id"] = str(row.pop("_id"))
    return row


def recent(limit: int = 200) -> list:
    return [_row(d) for d in db()[settings.AUDIT_COLLECTION].find().sort("_id", -1).limit(limit)]


def summary(since: str | None = None) -> dict:
    """Counts per step and decision, e.g. {"junk_check": {"junk": 3, "accepted": 10, "needs_review": 1}}.
    since: an ISO 8601 UTC timestamp; only rows at or after it are counted."""
    out = {}
    for g in db()[settings.AUDIT_COLLECTION].aggregate([
            {"$match": {"ts": {"$gte": since or ""}}},
            {"$group": {"_id": {"step": "$step", "decision": "$decision"}, "n": {"$sum": 1}}},
            {"$sort": {"_id.step": 1, "_id.decision": 1}}]):
        out.setdefault(g["_id"]["step"], {})[g["_id"]["decision"]] = g["n"]
    return out


# Steps of runs that start from a job, not from a file (the agent finding candidates for a job, or adding them to it).
# Such a run is not "a file that came in", so it is left out of the file counts below.
JOB_STEPS = ("find_candidates", "add_to_screening")


def run_count(since: str | None = None) -> int:
    """How many files came in (one run per file)."""
    counted = list(db()[settings.AUDIT_COLLECTION].aggregate([
        {"$match": {"ts": {"$gte": since or ""}, "step": {"$nin": list(JOB_STEPS)}}},
        {"$group": {"_id": "$run_id"}},
        {"$count": "n"}]))
    return counted[0]["n"] if counted else 0


def recent_runs(limit: int = 10) -> list:
    """The latest runs, newest first, one entry per file: when, which file, and each step's decision."""
    coll = db()[settings.AUDIT_COLLECTION]
    latest = [g["_id"] for g in coll.aggregate([
        {"$match": {"step": {"$nin": list(JOB_STEPS)}}},
        {"$group": {"_id": "$run_id", "last": {"$max": "$_id"}}},
        {"$sort": {"last": -1}},
        {"$limit": limit}])]
    runs = {}
    for r in coll.find({"run_id": {"$in": latest}}).sort("_id", 1):
        run = runs.setdefault(r["run_id"], {"run_id": r["run_id"], "ts": r["ts"], "channel": r["channel"],
                                            "file": r["file"], "record_id": None, "steps": {}})
        run["steps"][r["step"]] = r["decision"]
        run["record_id"] = r["record_id"] or run["record_id"]
        run["last_reason"] = r["reason"]
    return list(runs.values())[::-1]  # rows came oldest first, so runs did too
