"""The audit log: every step of an intake run writes one row to a local SQLite file (audit.db).

BRD: "Every step writes to the audit log, which feeds daily and weekly reports". One row per step per resume:
channel, timestamp, decision, reason and the Questlight record ID. Rows are only ever added, never changed.

What is NOT stored: the resume's text or any parsed candidate data (no email, phone, skills...). Only the file name,
a hash of the file, and the decision with its reason. File names often contain the candidate's name, so treat
audit.db as personal data.
"""
import hashlib
import json
import os
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path

from app import settings

CHANNEL = "manual upload"  # becomes "email" / "whatsapp" when those intake channels exist

SCHEMA = """
CREATE TABLE IF NOT EXISTS audit_log (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    ts        TEXT NOT NULL,   -- UTC, ISO 8601
    run_id    TEXT NOT NULL,   -- ties together the steps of one upload
    channel   TEXT NOT NULL,
    file      TEXT,
    file_hash TEXT,            -- sha256 of the file
    step      TEXT NOT NULL,   -- intake | junk_check | parse | load_questlight
    decision  TEXT NOT NULL,
    reason    TEXT,
    record_id TEXT,            -- Questlight applicant ID (CAN-...), once there is one
    details   TEXT             -- small JSON: rule signals, missing field names. Never candidate data
)"""


def db_path() -> Path:
    """The SQLite file: AUDIT_DB, else data/audit.db (or /tmp/audit.db on Vercel). The step traces (tracing.py) live in
    the same file. Read on every use, so a test or an operator can point it somewhere else without a restart."""
    return Path(os.getenv("AUDIT_DB") or settings.DEFAULT_AUDIT_DB)


def open_db() -> sqlite3.Connection:
    """A connection to the file, creating its folder first (a fresh checkout or a new Vercel instance has none)."""
    path = db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    return sqlite3.connect(path, timeout=5)


def _connect() -> sqlite3.Connection:
    conn = open_db()
    conn.execute(SCHEMA)
    return conn


class Run:
    """One uploaded file going through the pipeline. Call .log() at each step."""

    def __init__(self, file: str, data: bytes, channel: str = CHANNEL):
        self.run_id = uuid.uuid4().hex[:12]
        self.file = file
        self.file_hash = hashlib.sha256(data).hexdigest() if data else None
        self.channel = channel

    def log(self, step: str, decision: str, reason: str = "", record_id: str = None, **details) -> None:
        # A broken log must not stop intake, but it must be loud: a silent gap would make the reports wrong.
        try:
            conn = _connect()
            with conn:
                conn.execute(
                    "INSERT INTO audit_log (ts, run_id, channel, file, file_hash, step, decision, reason, record_id, details)"
                    " VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (datetime.now(timezone.utc).isoformat(timespec="seconds"), self.run_id, self.channel, self.file,
                     self.file_hash, step, decision, reason, record_id,
                     json.dumps(details, default=str) if details else None))
            conn.close()
        except (sqlite3.Error, OSError) as exc:  # OSError: the folder can't be created or the disk is read-only
            print(f"[audit] WARNING: couldn't write to the audit log ({exc})")


def recent(limit: int = 200) -> list:
    conn = _connect()
    conn.row_factory = sqlite3.Row
    rows = conn.execute("SELECT * FROM audit_log ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    conn.close()
    out = []
    for r in rows:
        row = dict(r)
        row["details"] = json.loads(row["details"]) if row["details"] else None
        out.append(row)
    return out


def summary(since: str = None) -> dict:
    """Counts per step and decision, e.g. {"junk_check": {"junk": 3, "accepted": 10, "needs_review": 1}}.
    since: an ISO 8601 UTC timestamp; only rows at or after it are counted."""
    conn = _connect()
    out = {}
    for step, decision, n in conn.execute("SELECT step, decision, COUNT(*) FROM audit_log WHERE ts >= ?"
                                          " GROUP BY step, decision", (since or "",)):
        out.setdefault(step, {})[decision] = n
    conn.close()
    return out


def run_count(since: str = None) -> int:
    """How many files came in (one run per file)."""
    conn = _connect()
    n = conn.execute("SELECT COUNT(DISTINCT run_id) FROM audit_log WHERE ts >= ?", (since or "",)).fetchone()[0]
    conn.close()
    return n


def recent_runs(limit: int = 10) -> list:
    """The latest runs, newest first, one entry per file: when, which file, and each step's decision."""
    conn = _connect()
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT * FROM audit_log WHERE run_id IN (SELECT run_id FROM audit_log GROUP BY run_id"
        " ORDER BY MAX(id) DESC LIMIT ?) ORDER BY id", (limit,)).fetchall()
    conn.close()
    runs = {}
    for r in rows:
        run = runs.setdefault(r["run_id"], {"run_id": r["run_id"], "ts": r["ts"], "channel": r["channel"],
                                            "file": r["file"], "record_id": None, "steps": {}})
        run["steps"][r["step"]] = r["decision"]
        run["record_id"] = r["record_id"] or run["record_id"]
        run["last_reason"] = r["reason"]
    return list(runs.values())[::-1]  # rows came oldest first, so runs did too
