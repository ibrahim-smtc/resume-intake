"""Every setting in one place. They come from environment variables; for local runs a .env file in the project root
is read too (real environment variables win over the file). On Vercel there is no .env file: set them in the project's
Environment Variables. Names and meanings are listed in .env.example.

Only settings that are fixed for the life of the process live here. A few are deliberately read at the moment they are
used, so they can change without a restart: QUESTLIGHT_* (app/intake/questlight.py), MCP_TOKEN and MCP_FILE_HOSTS
(app/agent), PERFOX_WEBHOOK_* (app/agent/chat_bridge.py), AUDIT_DB (app/observability/audit.py).
"""
import os
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
if not os.getenv("SKIP_DOTENV"):  # the tests set this, so they can never pick up real tokens from .env
    load_dotenv(ROOT / ".env")

ON_VERCEL = bool(os.getenv("VERCEL"))
STATIC_DIR = ROOT / "app" / "static"

# Resumes and JDs are read by Perfox's extract_structured (app/parsing/service.py).
PARSER = "perfox"

# Perfox API credentials
PERFOX_BASE_URL = os.getenv("PERFOX_BASE_URL", "").rstrip("/")
PERFOX_API_KEY = os.getenv("PERFOX_API_KEY", "")
# Telling a recruiter on WhatsApp how their fill-in form ended (app/agent/notify.py): the WhatsApp number the agent's trigger uses, and a
# Perfox key with the conversations:write scope (POST /messages): PERFOX_MESSAGING_KEY, else PERFOX_API_KEY. Without the number, nothing is sent.
PERFOX_MESSAGING_KEY = os.getenv("PERFOX_MESSAGING_KEY", "")
PERFOX_WHATSAPP_FROM = os.getenv("PERFOX_WHATSAPP_FROM", "").strip()

# Questlight's matching service (its own semantic matching), started for each job created from a JD.
MATCHING_BASE_URL = os.getenv("MATCHING_BASE_URL", "https://dev-api.quest-light.com/matching")
# How many of the best candidates a job created from a JD is screened with.
JD_SCREEN_TOP = int(os.getenv("JD_SCREEN_TOP") or 3)

# Masking is off until told otherwise. It covers Questlight's masked-PDF call and the redaction of the candidate JSON that
# the page and the agent see. The code for both is kept.
MASKING = os.getenv("MASKING_ENABLED", "false").lower() == "true"

# Largest resume accepted. Questlight's own bulk upload takes 5 MB, but a Vercel Function can't receive a body over 4.5 MB,
# so on Vercel the default is 4. MAX_UPLOAD_MB overrides either.
MAX_UPLOAD_MB = float(os.getenv("MAX_UPLOAD_MB") or (4 if ON_VERCEL else 5))
MAX_UPLOAD_BYTES = int(MAX_UPLOAD_MB * 1024 * 1024)
MAX_UPLOAD_LABEL = f"{MAX_UPLOAD_MB:g} MB"

# The address people open this app at, for the links it hands out (the fill-in form for missing details, which a WhatsApp
# recruiter opens on their phone). On Vercel the project's production address is used (VERCEL_PROJECT_PRODUCTION_URL, which
# Vercel sets itself; the per-deployment addresses ask for a Vercel login). Empty = links relative to this server, which is
# enough for the chat page, since it opens the form itself.
_VERCEL_HOST = os.getenv("VERCEL_PROJECT_PRODUCTION_URL", "").strip()
PUBLIC_BASE_URL = (os.getenv("PUBLIC_BASE_URL", "").strip() or (f"https://{_VERCEL_HOST}" if _VERCEL_HOST else "")).rstrip("/")

# Where the audit log and traces are kept (SQLite). On Vercel only /tmp is writable, and it does not survive between
# instances: see README.md, "Deploying to Vercel".
DEFAULT_AUDIT_DB = Path("/tmp/audit.db") if ON_VERCEL else ROOT / "data" / "audit.db"
