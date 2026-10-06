"""Shared helpers for the test scripts. Each tests/test_*.py is a plain script: run it directly, or run them all with
`python tests/run_all.py`. A script prints OK / BAD per check and "N/N checks passed", and exits non-zero on any BAD.

The tests are offline and never touch real services or your real .env: isolate() must be the first thing a script calls,
before it imports anything from `app`. Questlight and Perfox are small mock servers on free local ports; the resume parser
and Questlight's profile call are faked where a script needs them. Only test_live.py (opt-in, RUN_LIVE=1) talks to the
real dev services.
"""
import json
import os
import socket
import sys
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = ROOT / "tests" / "fixtures"
RESUMES = FIXTURES / "resumes"        # two fake resumes
JUNK_FILES = FIXTURES / "junk" / "files"  # 30 invented files for the junk rules (see junk/cases.json)

# Anything a real .env or shell could carry that a test must not inherit.
_REAL_SETTINGS = ("QUESTLIGHT_TOKEN", "QUESTLIGHT_BASE_URL", "QUESTLIGHT_ORIGIN", "QUESTLIGHT_ATTACH_RESUME", "PARSING_BASE_URL",
                  "PERFOX_API_KEY", "PERFOX_BASE_URL", "PERFOX_WEBHOOK_URL", "PERFOX_WEBHOOK_SECRET", "MCP_TOKEN", "MCP_FILE_HOSTS",
                  "APP_PASSWORD", "MAX_UPLOAD_MB", "VERCEL", "AUDIT_DB", "RESUME_PARSER", "MASKING_ENABLED")

_results = []


def isolate(**env) -> Path:
    """Call first. Clears real settings, points the audit database at a throwaway file, then applies `env`.
    Returns the temp folder."""
    sys.path.insert(0, str(ROOT))
    sys.path.insert(0, str(ROOT / "tests"))
    os.environ["SKIP_DOTENV"] = "1"
    for name in _REAL_SETTINGS:
        os.environ.pop(name, None)
    tmp = Path(tempfile.mkdtemp())
    os.environ["AUDIT_DB"] = str(tmp / "audit.db")
    os.environ.update({k: str(v) for k, v in env.items()})
    return tmp


def check(label: str, cond, extra="") -> bool:
    _results.append(bool(cond))
    print(("OK  " if cond else "BAD ") + label + (f"  [{extra}]" if extra and not cond else ""), flush=True)
    return bool(cond)


def finish() -> None:
    passed = sum(_results)
    print(f"\n{passed}/{len(_results)} checks passed", flush=True)
    sys.exit(0 if passed == len(_results) else 1)


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Served:
    """An ASGI app served on a free local port, in a background thread."""

    def __init__(self, application):
        import uvicorn
        self.port = free_port()
        self.url = f"http://127.0.0.1:{self.port}"
        self._server = uvicorn.Server(uvicorn.Config(application, host="127.0.0.1", port=self.port, log_level="warning"))
        threading.Thread(target=self._server.run, daemon=True).start()
        while not self._server.started:
            time.sleep(0.05)

    def stop(self) -> None:
        self._server.should_exit = True


def serve(application) -> Served:
    return Served(application)


def body_of(tool_result):
    """The JSON a tool returned, from an MCP client result. A tool that crashed is reported as such, with its message."""
    if tool_result.isError:
        raise AssertionError(f"the tool raised an error: {tool_result.content[0].text}")
    return tool_result.structuredContent if tool_result.structuredContent is not None else json.loads(tool_result.content[0].text)
