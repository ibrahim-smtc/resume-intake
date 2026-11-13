"""MongoDB execution span tracing for intake stages, plus the Perfox token usage of the LLM steps."""
import time
import uuid
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timezone

from pymongo.errors import ConnectionFailure, DuplicateKeyError

from app import settings
from app.observability import audit

KINDS = {"chain", "tool", "llm"}  # LangSmith run types: chain = a group of steps, tool = code or an API call

# Collections (names in settings.py; indexes are created in audit.db()):
#   trace_spans: one document per step of a run: trace_id (= audit run_id), span_id, parent_id (None for the root span),
#     seq (order the spans started in), name, kind (chain | tool | llm), start_ts (UTC, ISO 8601), offset_ms (from the start
#     of the trace), duration_ms, status (ok | error), error, provider (for llm spans: perfox...), model (only when the
#     provider reports it), input_tokens, output_tokens, details (small facts about the step. Never candidate data).
#   llm_perfox_usage_events: one document per LLM step (see _record_usage).
#   llm_perfox_usage_daily: the usage rolled up per day, provider and model.

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
        """Writes the spans, and the Perfox usage of the LLM steps, to MongoDB. Never raises: a broken trace store must not fail
        the upload, but it says so loudly. Also prints one [timing] line, so the run's timings can be read in the host's logs
        too."""
        if not self.spans:
            return
        try:
            print(timing_line(self))
        except Exception:  # a timing line must never fail the run either
            pass
        try:
            database = audit.db()
            database[settings.TRACE_COLLECTION].insert_many(
                [{"trace_id": self.id, "span_id": s.id, "parent_id": s.parent_id, "seq": s.seq, "name": s.name, "kind": s.kind,
                  "start_ts": s.started.isoformat(timespec="milliseconds"), "offset_ms": s.offset_ms,
                  "duration_ms": s.duration_ms, "status": s.status, "error": s.error, "provider": s.provider, "model": s.model,
                  "input_tokens": s.input_tokens, "output_tokens": s.output_tokens, "details": audit.plain(s.details)}
                 for s in self.spans], ordered=False)
        except audit.STORE_ERRORS as exc:
            print(f"[trace] WARNING: couldn't save the trace ({audit.short(exc)})")
            if isinstance(exc, ConnectionFailure):
                return  # MongoDB can't be reached: the usage write would only wait for it as well
        try:
            _record_usage(audit.db(), self)
        except audit.STORE_ERRORS as exc:
            print(f"[usage] WARNING: couldn't record the Perfox usage ({audit.short(exc)})")


def _record_usage(database, trace) -> None:
    """The Perfox usage of a run: one event per LLM step, and the day's totals. The event goes in first (a trace and step can be
    counted only once), and the totals grow only when that insert worked, so saving a trace twice can't count it twice.
    Tokens are null when Perfox didn't report them: such a call is counted in calls_without_usage, not as zero tokens."""
    events = database[settings.USAGE_EVENTS_COLLECTION]
    daily = database[settings.USAGE_DAILY_COLLECTION]
    channel = trace.spans[0].details.get("channel")
    for s in trace.spans:
        if s.kind != "llm":
            continue
        reported = s.input_tokens is not None or s.output_tokens is not None
        provider, model = s.provider or "perfox", s.model or "unreported"
        day = s.started.astimezone(timezone.utc).strftime("%Y-%m-%d")
        tokens_in, tokens_out = s.input_tokens or 0, s.output_tokens or 0
        try:
            events.insert_one({"ts": s.started, "day": day, "trace_id": trace.id, "span_id": s.id, "step": s.name,
                               "provider": provider, "model": model, "input_tokens": s.input_tokens, "output_tokens": s.output_tokens,
                               "total_tokens": tokens_in + tokens_out if reported else None, "usage_reported": reported,
                               "duration_ms": s.duration_ms, "status": s.status, "error": s.error, "channel": channel})
        except DuplicateKeyError:
            continue
        daily.update_one({"day": day, "provider": provider, "model": model},
                         {"$inc": {"calls": 1, "calls_without_usage": 0 if reported else 1, "errors": 1 if s.status == "error" else 0,
                                   "input_tokens": tokens_in, "output_tokens": tokens_out, "total_tokens": tokens_in + tokens_out,
                                   "duration_ms_total": s.duration_ms or 0},
                          "$set": {"updated_at": datetime.now(timezone.utc)}}, upsert=True)


# ---------- where the time went ----------

# For "which hop is slow?": reading the document is Perfox, this app's own checks cost almost nothing, and every other step
# is a call to Questlight.
PERFOX_STEPS = {"parse resume", "parse JD"}
APP_STEPS = {"intake checks", "junk check", "what is it"}
PARALLEL_STEP = "fetch open jobs"  # started early, runs while the document is being read (see pipeline.run_pipeline)


def _direct_steps(spans: list) -> list:
    """The steps directly under the root span, in order. Steps inside them are already part of their time."""
    root = spans[0] if spans else None
    return [s for s in spans[1:] if root and s["parent_id"] == root["id"] and s["duration_ms"] is not None]


def stage_seconds(summary):
    """Where one run's time went, from Trace.summary(): the whole run and the part spent in Perfox, in Questlight and in this
    app's own steps, in seconds. The early open-jobs download is left out because it overlaps the Perfox step, so counting
    it would count that time twice. None when there is no finished trace."""
    spans = (summary or {}).get("spans") or []
    if not spans or spans[0]["duration_ms"] is None:
        return None
    hops = {"perfox": 0.0, "questlight": 0.0, "app": 0.0}
    for s in _direct_steps(spans):
        if s["name"] == PARALLEL_STEP:
            continue
        hops["perfox" if s["name"] in PERFOX_STEPS else "app" if s["name"] in APP_STEPS else "questlight"] += s["duration_ms"] / 1000
    return {"total": round(spans[0]["duration_ms"] / 1000, 1), **{hop: round(sec, 1) for hop, sec in hops.items()}}


def timing_line(trace) -> str:
    """One line for the logs: the run, its channel, the seconds in each hop and in each step. Step names and seconds only."""
    spans = trace.summary()["spans"]
    hops = stage_seconds({"spans": spans}) or {}
    steps = [f"{s['name']} {s['duration_ms'] / 1000:.1f}s" + (" (parallel)" if s["name"] == PARALLEL_STEP else "")
             for s in _direct_steps(spans)]
    return (f"[timing] {trace.id} {spans[0]['name']} channel={spans[0]['details'].get('channel', '?')} total={hops.get('total')}s "
            f"perfox={hops.get('perfox')}s questlight={hops.get('questlight')}s app={hops.get('app')}s | " + " | ".join(steps))


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

def _row(doc: dict) -> dict:
    d = dict(doc)
    d.pop("_id", None)
    d["id"] = d["span_id"]   # parent_id points at a span_id, so the viewer can only nest the steps with this as the id
    d["details"] = d.get("details") or {}
    return d


def get_trace(trace_id: str):
    database = audit.db()
    rows = [_row(d) for d in database[settings.TRACE_COLLECTION].find({"trace_id": trace_id}).sort("seq", 1)]
    if not rows:
        return None
    # A trace id is the audit run id (12 characters), plus "-<tool>" for a single step called by the agent
    logged = database[settings.AUDIT_COLLECTION].find_one({"run_id": trace_id[:12]})
    root = rows[0]
    return {"trace_id": trace_id, "file": logged["file"] if logged else None, "start_ts": root["start_ts"],
            "total_ms": root["duration_ms"], "status": "error" if any(r["status"] == "error" for r in rows) else "ok",
            "input_tokens": sum(r["input_tokens"] or 0 for r in rows), "output_tokens": sum(r["output_tokens"] or 0 for r in rows),
            "llm_calls": sum(1 for r in rows if r["kind"] == "llm"), "spans": rows}


def recent_traces(limit: int = 100) -> list:
    database = audit.db()
    spans = database[settings.TRACE_COLLECTION]
    roots = list(spans.find({"parent_id": None}).sort("_id", -1).limit(limit))
    ids = [r["trace_id"] for r in roots]
    totals = {g["_id"]: g for g in spans.aggregate([
        {"$match": {"trace_id": {"$in": ids}}},
        {"$group": {"_id": "$trace_id", "input_tokens": {"$sum": {"$ifNull": ["$input_tokens", 0]}},
                    "output_tokens": {"$sum": {"$ifNull": ["$output_tokens", 0]}},
                    "errors": {"$sum": {"$cond": [{"$eq": ["$status", "error"]}, 1, 0]}}}}])}
    files = {}
    for a in database[settings.AUDIT_COLLECTION].find({"run_id": {"$in": list({i[:12] for i in ids})}}).sort("_id", 1):
        files.setdefault(a["run_id"], a["file"])
    return [{"trace_id": r["trace_id"], "start_ts": r["start_ts"], "total_ms": r["duration_ms"],
             "input_tokens": totals[r["trace_id"]]["input_tokens"], "output_tokens": totals[r["trace_id"]]["output_tokens"],
             "errors": totals[r["trace_id"]]["errors"], "file": files.get(r["trace_id"][:12])} for r in roots]


def step_stats() -> list:
    """Per step name: how often it ran, its average and slowest time, and the tokens it used in total."""
    groups = audit.db()[settings.TRACE_COLLECTION].aggregate([
        {"$group": {"_id": {"name": "$name", "kind": "$kind"}, "runs": {"$sum": 1}, "avg_ms": {"$avg": "$duration_ms"},
                    "max_ms": {"$max": "$duration_ms"}, "input_tokens": {"$sum": {"$ifNull": ["$input_tokens", 0]}},
                    "output_tokens": {"$sum": {"$ifNull": ["$output_tokens", 0]}},
                    "errors": {"$sum": {"$cond": [{"$eq": ["$status", "error"]}, 1, 0]}}, "first_seq": {"$min": "$seq"}}},
        {"$sort": {"first_seq": 1, "_id.name": 1}}])
    return [{"name": g["_id"]["name"], "kind": g["_id"]["kind"], "runs": g["runs"], "avg_ms": g["avg_ms"], "max_ms": g["max_ms"],
             "input_tokens": g["input_tokens"], "output_tokens": g["output_tokens"], "errors": g["errors"],
             "first_seq": g["first_seq"]} for g in groups]
