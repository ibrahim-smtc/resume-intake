"""Common setup for the agent tests: the real app and MCP server, served on a free port, with
- the resume parser faked (it returns hand-made JSON chosen by the file's name),
- Questlight's profile creation recorded and stubbed (nothing is written anywhere),
- resume "downloads" read from disk (https://files.test/<name>) and the job list pre-filled.

Call after harness.isolate(). The tests then talk to /mcp with the real MCP client, exactly as Perfox does."""
import contextlib
import copy
import os
from pathlib import Path

import harness
from harness import RESUMES, JUNK_FILES, body_of, serve


class Fakes:
    def __init__(self):
        self.parse_calls = []      # the file names the parser was asked to read
        self.creates = []          # the payloads "sent" to Questlight
        self.create_mode = "ok"    # "ok" | "fail_once" | "duplicate"
        self.url = self.headers = self.server = None


def start(parsed_by_stem: dict, files: dict = None, token: str = "test-token-123") -> Fakes:
    """parsed_by_stem: file stem -> parsed JSON (or a string: Questlight's "couldn't find the details" message).
    files: stem -> bytes served for https://files.test/<stem> (default: a fake resume that passes the junk check)."""
    os.environ["MCP_TOKEN"] = token
    from app.agent import downloads
    from app.intake import matching, parser_questlight, questlight
    from app.main import app
    from fixtures import jobs

    fakes = Fakes()
    resume = (RESUMES / "Rohit_Verma_Python_Backend.pdf").read_bytes()
    served = {"invoice": (JUNK_FILES / "j06_invoice.pdf").read_bytes(), **(files or {})}

    async def fake_parse(client, name, data, ext):
        fakes.parse_calls.append(name)
        stem = Path(name).stem.lower()
        key = next((k for k in parsed_by_stem if stem.startswith(k.lower())), None)   # "Rohit_Verma.pdf" finds "rohit"
        if key is None:
            raise KeyError(f"no fake parser output registered for {name!r} (have {sorted(parsed_by_stem)})")
        entry = parsed_by_stem[key]
        return (None, entry) if isinstance(entry, str) else (copy.deepcopy(entry), None)

    async def fake_create(applicant, name, data, ext):
        fakes.creates.append(applicant)
        if fakes.create_mode == "duplicate":
            return {"status": "duplicate", "message": "a profile with this email already exists in Questlight, nothing was changed"}
        if fakes.create_mode == "fail_once":
            fakes.create_mode = "ok"
            return {"status": "failed", "message": "Questlight didn't answer within 60s, no profile was confirmed"}
        return {"status": "created", "message": "stubbed: nothing was written", "applicantId": f"CAN-STUB-{len(fakes.creates)}"}

    real_download = downloads.download

    async def fake_download(url, file_name):
        if url.startswith("https://files.test/"):
            stem = url.rsplit("/", 1)[-1]
            data = served.get(stem, resume)
            return data, downloads.file_name_for(file_name or stem, data), None
        return await real_download(url, file_name)

    parser_questlight.parse = fake_parse
    questlight.create_profile = fake_create
    downloads.download = fake_download
    jobs.prime(matching)

    fakes.server = serve(app)
    fakes.url = fakes.server.url + "/mcp"
    fakes.headers = {"Authorization": f"Bearer {token}"}
    return fakes


@contextlib.asynccontextmanager
async def session(fakes: Fakes):
    """An MCP client session, like Perfox's."""
    from mcp import ClientSession
    from mcp.client.streamable_http import streamablehttp_client
    async with streamablehttp_client(fakes.url, headers=fakes.headers) as (read, write, _):
        async with ClientSession(read, write) as s:
            await s.initialize()
            yield s


async def call(s, tool: str, **arguments) -> dict:
    return body_of(await s.call_tool(tool, arguments))


__all__ = ["Fakes", "start", "session", "call", "harness"]
