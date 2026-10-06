"""Step tracing, in the style of LangSmith: every upload is a trace, and every step or API call inside it is a span with its
start, duration, status and, for LLM calls, the tokens it used.

Usage, from any module (a span opened outside a trace does nothing, so modules work on their own too):

    with tracing.span("fetch open jobs") as s:
        ...
        s.set(open_jobs=330)                   # small facts about the step
        s.fail("job list returned HTTP 500")   # marks the step as failed

    tracing.record_usage(370, 571, provider="perfox")   # inside an LLM span: the tokens it used

Spans are saved to the trace_spans table in audit.db (same file as the audit log; trace_id = the audit run_id).
Like the audit log they hold no candidate data: names, timings, token counts and small counts or statuses only.

"""
import json
import sqlite3
import time
import uuid
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timezone

from app.observability import audit

KINDS = {"chain", "tool", "llm"}  # LangSmith run types: chain = a group of steps, tool = code or an API call

SCHEMA = """
CREATE TABLE IF NOT EXISTS trace_spans (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    trace_id      TEXT NOT NULL,   -- = audit_log.run_id
    span_id       TEXT NOT NULL,
    parent_id     TEXT,            -- NULL for the root span of the trace
    seq           INTEGER NOT NULL, -- order the spans started in
    name          TEXT NOT NULL,
    kind          TEXT NOT NULL,   -- chain | tool | llm
    start_ts      TEXT NOT NULL,   -- UTC, ISO 8601
    offset_ms     REAL NOT NULL,   -- start, counted from the start of the trace
    duration_ms   REAL,
    status        TEXT NOT NULL,   -- ok | error
    error         TEXT,
    provider      TEXT,            -- for llm spans: perfox, questlight...
    model         TEXT,            -- only when the provider reports it
    input_tokens  INTEGER,
    output_tokens INTEGER,
    details       TEXT             -- small JSON of facts about the step. Never candidate data
)"""

_current: ContextVar = ContextVar("current_span", default=None)


class Span:
    def __init__(self, trace, name: str, kind: str, parent):
        self.trace = trace
        self.id = uuid.uuid4().hex[:12]
        self.parent_id = parent.id if parent else None
        self.seq = len(trace.spans)
        self.name = name
        self.kind = kind if kind in KINDS else "tool"
        self.started = datetime.now(timezone.utc)
        self._t0 = time.perf_counter()
        self.duration_ms = None
        self.status, self.error = "ok", None
        self.provider = self.model = None
        self.input_tokens = self.output_tokens = None
        self.details = {}

    def set(self, **details) -> None:
        self.details.update(details)

    def fail(self, message: str) -> None:
        self.status, self.error = "error", str(message)[:500]

    def set_usage(self, input_tokens=None, output_tokens=None, provider=None, model=None) -> None:
        self.input_tokens = int(input_tokens) if input_tokens is not None else None
        self.output_tokens = int(output_tokens) if output_tokens is not None else None
        self.provider = provider or self.provider
        self.model = model or self.model

    @property
    def offset_ms(self) -> float:
        return (self._t0 - self.trace.spans[0]._t0) * 1000

    def to_dict(self) -> dict:
        return {"id": self.id, "parent_id": self.parent_id, "name": self.name, "kind": self.kind,
                "start_ts": self.started.isoformat(timespec="milliseconds"), "offset_ms": round(self.offset_ms, 1),
                "duration_ms": round(self.duration_ms, 1) if self.duration_ms is not None else None,
                "status": self.status, "error": self.error, "provider": self.provider, "model": self.model,
                "input_tokens": self.input_tokens, "output_tokens": self.output_tokens, "details": self.details}


class _NoSpan:
    """Stands in for a span when there is no trace running, so callers never need to check."""

    def set(self, **details): pass
    def fail(self, message): pass
    def set_usage(self, *args, **kwargs): pass


class Trace:
    """One upload. The first span opened is the root; spans opened inside it become its children."""

    def __init__(self, trace_id: str):
        self.id = trace_id
        self.spans = []

    @contextmanager
    def span(self, name: str, kind: str = "tool"):
        parent = _current.get()
        s = Span(self, name, kind, parent if isinstance(parent, Span) and parent.trace is self else None)
        self.spans.append(s)
        token = _current.set(s)
        try:
            yield s
        except Exception as exc:
            s.fail(f"{type(exc).__name__}: {exc}")
            raise
        finally:
            s.duration_ms = (time.perf_counter() - s._t0) * 1000
            _current.reset(token)

    def summary(self) -> dict:
        llm = [s for s in self.spans if s.kind == "llm"]
        root = self.spans[0] if self.spans else None
        return {"trace_id": self.id,
                "total_ms": round(root.duration_ms, 1) if root and root.duration_ms is not None else None,
                "input_tokens": sum(s.input_tokens or 0 for s in self.spans),
                "output_tokens": sum(s.output_tokens or 0 for s in self.spans),
                "llm_calls": len(llm), "llm_calls_without_usage": sum(1 for s in llm if s.input_tokens is None),
                "spans": [s.to_dict() for s in self.spans]}

    def save(self) -> None:
        """Writes the spans to the audit database. Never raises: a broken trace store must not fail the upload, but it
        says so loudly."""
        if not self.spans:
            return
        try:
            conn = _connect()
            with conn:
                conn.executemany(
                    "INSERT INTO trace_spans (trace_id, span_id, parent_id, seq, name, kind, start_ts, offset_ms, duration_ms,"
                    " status, error, provider, model, input_tokens, output_tokens, details)"
                    " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    [(self.id, s.id, s.parent_id, s.seq, s.name, s.kind, s.started.isoformat(timespec="milliseconds"),
                      s.offset_ms, s.duration_ms, s.status, s.error, s.provider, s.model, s.input_tokens, s.output_tokens,
                      json.dumps(s.details, default=str) if s.details else None) for s in self.spans])
            conn.close()
        except (sqlite3.Error, OSError) as exc:
            print(f"[trace] WARNING: couldn't save the trace ({exc})")


# ---------- helpers for the pipeline modules ----------

@contextmanager
def span(name: str, kind: str = "tool"):
    """A child span of whatever span is running now; does nothing outside a trace."""
    parent = _current.get()
    if not isinstance(parent, Span):
        yield _NoSpan()
        return
    with parent.trace.span(name, kind) as s:
        yield s


def current():
    s = _current.get()
    return s if isinstance(s, Span) else _NoSpan()


def record_usage(input_tokens=None, output_tokens=None, provider=None, model=None) -> None:
    """Tokens used by the LLM call in the current span (like LangSmith's usage_metadata)."""
    current().set_usage(input_tokens, output_tokens, provider, model)


# ---------- reading traces back ----------

def _connect() -> sqlite3.Connection:
    conn = audit.open_db()
    conn.execute(SCHEMA)
    conn.row_factory = sqlite3.Row
    return conn


def _row(r) -> dict:
    d = dict(r)
    d["details"] = json.loads(d["details"]) if d["details"] else {}
    return d


def get_trace(trace_id: str):
    conn = _connect()
    rows = [_row(r) for r in conn.execute("SELECT * FROM trace_spans WHERE trace_id = ? ORDER BY seq", (trace_id,))]
    # A trace id is the audit run id (12 characters), plus "-<tool>" for a single step called by the agent
    file = conn.execute("SELECT file FROM audit_log WHERE run_id = ? LIMIT 1", (trace_id[:12],)).fetchone() if _has_audit(conn) else None
    conn.close()
    if not rows:
        return None
    root = rows[0]
    return {"trace_id": trace_id, "file": file["file"] if file else None, "start_ts": root["start_ts"],
            "total_ms": root["duration_ms"], "status": "error" if any(r["status"] == "error" for r in rows) else "ok",
            "input_tokens": sum(r["input_tokens"] or 0 for r in rows), "output_tokens": sum(r["output_tokens"] or 0 for r in rows),
            "llm_calls": sum(1 for r in rows if r["kind"] == "llm"), "spans": rows}


def _has_audit(conn) -> bool:
    return bool(conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='audit_log'").fetchone())


def recent_traces(limit: int = 100) -> list:
    conn = _connect()
    rows = conn.execute(
        "SELECT r.trace_id, r.start_ts, r.duration_ms AS total_ms,"
        " (SELECT SUM(COALESCE(input_tokens,0)) FROM trace_spans s WHERE s.trace_id = r.trace_id) AS input_tokens,"
        " (SELECT SUM(COALESCE(output_tokens,0)) FROM trace_spans s WHERE s.trace_id = r.trace_id) AS output_tokens,"
        " (SELECT COUNT(*) FROM trace_spans s WHERE s.trace_id = r.trace_id AND s.status = 'error') AS errors"
        + (", (SELECT file FROM audit_log a WHERE a.run_id = substr(r.trace_id, 1, 12) LIMIT 1) AS file" if _has_audit(conn) else ", NULL AS file")
        + " FROM trace_spans r WHERE r.parent_id IS NULL ORDER BY r.id DESC LIMIT ?", (limit,)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def step_stats() -> list:
    """Per step name: how often it ran, its average and slowest time, and the tokens it used in total."""
    conn = _connect()
    rows = conn.execute(
        "SELECT name, kind, COUNT(*) AS runs, AVG(duration_ms) AS avg_ms, MAX(duration_ms) AS max_ms,"
        " SUM(COALESCE(input_tokens,0)) AS input_tokens, SUM(COALESCE(output_tokens,0)) AS output_tokens,"
        " SUM(CASE WHEN status = 'error' THEN 1 ELSE 0 END) AS errors, MIN(seq) AS first_seq"
        " FROM trace_spans GROUP BY name, kind ORDER BY first_seq, name").fetchall()
    conn.close()
    return [dict(r) for r in rows]
