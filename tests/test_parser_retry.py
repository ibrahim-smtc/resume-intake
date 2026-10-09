"""Questlight's resume parser client: a quick 502/503/504 is retried once after a pause, other failures are not, and the
message for a down service tells the agent not to retry. The parsing service is a mock; nothing real is called."""
import harness

harness.isolate()
from harness import check, finish, serve  # noqa: E402

import asyncio  # noqa: E402
import os  # noqa: E402

from fastapi import FastAPI, UploadFile  # noqa: E402
from fastapi.responses import JSONResponse  # noqa: E402

mock = FastAPI()
script, seen, delay = [], [], [0]


@mock.post("/parsing/resume")
async def resume(pdf_doc: UploadFile):
    await pdf_doc.read()
    seen.append(1)
    await asyncio.sleep(delay[0])
    status = script.pop(0) if script else 200
    return JSONResponse({"name": "Test Person"} if status == 200 else {"detail": "down"}, status)


server = serve(mock)
os.environ["PARSING_BASE_URL"] = server.url + "/parsing"   # read when settings is imported: set before the app code loads

import httpx  # noqa: E402

from app.intake import parser_questlight  # noqa: E402

parser_questlight.RETRY_PAUSE_S = 0.05
parser_questlight.RETRY_IF_WITHIN_S = 0.4


def parse():
    async def go():
        async with httpx.AsyncClient(timeout=30) as client:
            return await parser_questlight.parse(client, "a.pdf", b"%PDF-1.4 x", ".pdf")
    seen.clear()
    return asyncio.run(go())


script[:] = [503]
parsed, err = parse()
check("a 503 then a good answer: retried once, and the resume is read", parsed == {"name": "Test Person"} and err is None and len(seen) == 2, (err, len(seen)))
script[:] = [503, 503]
parsed, err = parse()
check("503 twice: gives up after the one retry, saying the service is down and not to retry now",
      parsed is None and len(seen) == 2 and "HTTP 503" in err and "Don't retry now" in err and "in a minute" in err, (err, len(seen)))
script[:] = [504]
check("a 504 is retried too", parse()[0] == {"name": "Test Person"} and len(seen) == 2)
script[:] = [503, 503]
delay[0] = 0.6
parsed, err = parse()
delay[0] = 0
check("a 503 that arrives only after a long wait is NOT retried (the second try would cost as long again)",
      parsed is None and len(seen) == 1 and "HTTP 503" in err and "Don't retry now" in err, (err, len(seen)))
script[:] = [520]
parsed, err = parse()
check("a 520 (slow to arrive) is NOT retried, but is explained", parsed is None and len(seen) == 1 and "HTTP 520" in err and "Don't retry now" in err, (err, len(seen)))
script[:] = [400]
parsed, err = parse()
check("a 4xx is not retried and gets no 'service is down' advice", parsed is None and len(seen) == 1 and err == "parser returned HTTP 400", (err, len(seen)))
script[:] = []
parsed, err = parse()
check("a good answer is not retried", parsed == {"name": "Test Person"} and len(seen) == 1)
finish()
