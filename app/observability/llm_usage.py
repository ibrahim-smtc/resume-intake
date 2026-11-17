"""Perfox LLM usage written to Questlight's shared usage collections (llm_usage_events, llm_usage_daily)."""
import json
import os
import queue
import threading
import time
from datetime import datetime, timezone

from pymongo import MongoClient, UpdateOne
from pymongo.errors import BulkWriteError, ConfigurationError

from app.intake import questlight
from app.observability import audit

SCHEMA_VERSION = 1
EVENTS, DAILY = "llm_usage_events", "llm_usage_daily"
STATUSES = ("ok", "error", "rate_limited", "timeout", "connection", "cancelled")
ROLLUP_KEY = ("day", "tenant_id", "service", "operation", "model")
DUPLICATE_KEY = 11000
RETRY_IF_FAILED_WITHIN_S = 1.0
SKIP_AFTER_FAILURE_S = 30

_lock = threading.Lock()
_queue: queue.Queue = queue.Queue()
_thread = None
_pid = None
_clients: dict = {}
_skip_until = 0.0
_dropped = 0


def _env(name: str, default: str = "") -> str:
    return (os.getenv(name) or "").strip() or default


def _number(name: str, default: float) -> float:
    try:
        return float(_env(name) or default)
    except ValueError:
        return default


def enabled() -> bool:
    return bool(_env("LLM_USAGE_MONGO_URL") and _env("LLM_USAGE_DB_NAME")) and _env("LLM_USAGE_ENABLED", "true").lower() != "false"


def operation_name(kind: str) -> str:
    if kind == "jd":
        return _env("LLM_USAGE_OPERATION_JD", "jd.extract")
    return _env("LLM_USAGE_OPERATION_RESUME", "resume.extract")


def record(*, operation: str, status: str, latency_ms: int | None, http_status: int | None = None, error_type: str | None = None,
           prompt_tokens: int | None = None, completion_tokens: int | None = None, model: str | None = None,
           model_served: str | None = None, request_id: str | None = None) -> None:
    global _dropped
    if not enabled():
        return
    try:
        claims = questlight.token_claims()
        tenant = _env("LLM_USAGE_TENANT_ID") or claims.get("tenant_id") or None
        user = claims.get("user_id") or None
        event = {
            "schema_version": SCHEMA_VERSION,
            "ts": datetime.now(timezone.utc),
            "service": _env("LLM_USAGE_SERVICE_NAME", "resume-agent-mcp"),
            "operation": operation,
            "provider": _env("LLM_USAGE_PROVIDER", "perfox"),
            "model": model or "unreported",
            "model_served": model_served,
            "attempt": 1,
            "status": status if status in STATUSES else "error",
            "error_type": error_type,
            "http_status": http_status,
            "latency_ms": latency_ms,
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "cached_tokens": None,
            "finish_reason": None,
            "max_tokens": None,
            "openai_request_id": None,
            "tenant_id": str(tenant) if tenant is not None else None,
            "user_id": str(user) if user is not None else None,
            "request_id": request_id,
        }
        _start()
        if _queue.qsize() >= _number("LLM_USAGE_QUEUE_MAX", 10000):
            _dropped += 1
            if _dropped == 1 or _dropped % 1000 == 0:
                print(f"[llm-usage] WARNING: the usage queue is full; {_dropped} events dropped so far", flush=True)
            return
        _queue.put_nowait(event)
    except Exception as exc:
        print(f"[llm-usage] WARNING: couldn't queue a usage event ({audit.short(exc)})", flush=True)


def flush(timeout_s: float = 10) -> None:
    if _thread is None or not _thread.is_alive() or _pid != os.getpid():
        return
    done = threading.Event()
    _queue.put(done)
    done.wait(timeout_s)


def _start() -> None:
    global _thread, _pid, _queue
    with _lock:
        if _pid != os.getpid():
            _pid, _queue, _thread = os.getpid(), queue.Queue(), None
            _clients.clear()
        if _thread is None or not _thread.is_alive():
            _thread = threading.Thread(target=_run, name="llm-usage-writer", daemon=True)
            _thread.start()


def _run() -> None:
    while True:
        batch, waiting = _collect()
        try:
            if batch:
                _flush(batch)
        except Exception as exc:
            every = list(range(len(batch)))
            _give_up(batch, every, every, audit.short(exc))
        finally:
            for done in waiting:
                done.set()


def _collect() -> tuple[list, list]:
    item = _queue.get()
    batch: list = []
    size = max(1, int(_number("LLM_USAGE_BATCH_SIZE", 200)))
    deadline = time.monotonic() + _number("LLM_USAGE_FLUSH_SECONDS", 5)
    while True:
        if isinstance(item, threading.Event):
            return batch, [item]
        batch.append(item)
        if len(batch) >= size:
            return batch, []
        try:
            item = _queue.get(timeout=max(0.0, deadline - time.monotonic()))
        except queue.Empty:
            return batch, []


def _database():
    url = _env("LLM_USAGE_MONGO_URL")
    client = _clients.get(url)
    if client is None:
        try:
            client = _clients[url] = MongoClient(url, appname="questlight-resume-intake", **audit._timeouts(url))
        except ValueError:
            raise ConfigurationError("LLM_USAGE_MONGO_URL is not a valid connection string") from None
    return client[_env("LLM_USAGE_DB_NAME")]


def _flush(events: list) -> None:
    every = list(range(len(events)))
    if time.monotonic() < _skip_until:
        _give_up(events, every, every, "MongoDB is skipped for a while after a failed write")
        return
    try:
        database = _database()
    except audit.STORE_ERRORS as exc:
        _give_up(events, every, every, audit.short(exc))
        return
    unsaved, error = _with_retry(_inserter(database[EVENTS]), events)
    if error is not None and len(unsaved) == len(events):
        _give_up(events, every, every, audit.short(error))
        return
    ops, members = _rollups(events)
    failed, rollup_error = _with_retry(_bulk_writer(database[DAILY]), ops)
    unrolled = sorted(i for op in failed for i in members[op])
    if unsaved or unrolled:
        problem = error or rollup_error
        _give_up(events, unsaved, unrolled, audit.short(problem) if problem else "some documents were refused")


def _with_retry(write, items: list) -> tuple[list, Exception | None]:
    left, error = list(range(len(items))), None
    for attempt in (1, 2):
        started = time.monotonic()
        try:
            failed, error = write([items[i] for i in left])
        except audit.STORE_ERRORS as exc:
            failed, error = set(range(len(left))), exc
        left = [left[i] for i in sorted(failed)]
        if not left or attempt == 2 or time.monotonic() - started >= RETRY_IF_FAILED_WITHIN_S:
            break
    return left, error


def _inserter(collection):
    def write(events: list) -> tuple[set, Exception | None]:
        try:
            collection.insert_many(events, ordered=False)
            return set(), None
        except BulkWriteError as exc:
            failed = {e["index"] for e in exc.details.get("writeErrors", []) if e.get("code") != DUPLICATE_KEY}
            return failed, exc if failed else None
    return write


def _bulk_writer(collection):
    def write(ops: list) -> tuple[set, Exception | None]:
        try:
            collection.bulk_write(ops, ordered=False)
            return set(), None
        except BulkWriteError as exc:
            failed = {e["index"] for e in exc.details.get("writeErrors", [])}
            return failed, exc if failed else None
    return write


def _rollups(events: list) -> tuple[list, list]:
    groups: dict = {}
    for i, e in enumerate(events):
        day = e["ts"].replace(hour=0, minute=0, second=0, microsecond=0)
        groups.setdefault((day, e["tenant_id"] or "unknown", e["service"], e["operation"], e["model"]), []).append(i)
    now = datetime.now(timezone.utc)
    ops = []
    for key, members in groups.items():
        rows = [events[i] for i in members]
        latencies = [r["latency_ms"] for r in rows if r["latency_ms"] is not None]
        ops.append(UpdateOne(dict(zip(ROLLUP_KEY, key)), {
            "$inc": {"calls": len(rows),
                     "ok": sum(r["status"] == "ok" for r in rows),
                     "failed": sum(r["status"] != "ok" for r in rows),
                     "prompt_tokens": sum(r["prompt_tokens"] or 0 for r in rows),
                     "completion_tokens": sum(r["completion_tokens"] or 0 for r in rows),
                     "cached_tokens": sum(r["cached_tokens"] or 0 for r in rows),
                     "latency_ms_sum": sum(latencies)},
            "$max": {"latency_ms_max": max(latencies, default=0)},
            "$set": {"updated_at": now}}, upsert=True))
    return ops, list(groups.values())


def _plain(value) -> str:
    return value.isoformat() if isinstance(value, datetime) else str(value)


def _give_up(events: list, unsaved, unrolled, why: str) -> None:
    global _skip_until
    _skip_until = time.monotonic() + SKIP_AFTER_FAILURE_S
    unsaved, unrolled = set(unsaved), set(unrolled)
    lost = sorted(unsaved | unrolled)
    print(f"[llm-usage] WARNING: couldn't write {len(lost)} usage events to MongoDB ({why}); they follow as LLM_USAGE_EVENT lines, "
          f"and MongoDB is skipped for {SKIP_AFTER_FAILURE_S} s", flush=True)
    for i in lost:
        not_written = [name for name, missing in ((EVENTS, i in unsaved), (DAILY, i in unrolled)) if missing]
        print("LLM_USAGE_EVENT " + json.dumps({**events[i], "not_written": not_written}, default=_plain), flush=True)
