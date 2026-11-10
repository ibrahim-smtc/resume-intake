"""Application configuration loaded from environment variables and local .env files."""
import os
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
if not os.getenv("SKIP_DOTENV"):  # Allows isolated runs to skip loading local .env
    load_dotenv(ROOT / ".env")

ON_VERCEL = bool(os.getenv("VERCEL"))
STATIC_DIR = ROOT / "app" / "static"

# Resumes and JDs are read by Perfox's extract_structured (app/parsing/service.py).
PARSER = "perfox"

# Perfox API credentials
PERFOX_BASE_URL = os.getenv("PERFOX_BASE_URL", "").rstrip("/")
PERFOX_API_KEY = os.getenv("PERFOX_API_KEY", "")
# WhatsApp notification settings for completed fill-in forms (PERFOX_MESSAGING_KEY overrides PERFOX_API_KEY).
PERFOX_MESSAGING_KEY = os.getenv("PERFOX_MESSAGING_KEY", "")
PERFOX_WHATSAPP_FROM = os.getenv("PERFOX_WHATSAPP_FROM", "").strip()

# Questlight's matching service (its own semantic matching), started for each job created from a JD.
MATCHING_BASE_URL = os.getenv("MATCHING_BASE_URL", "https://dev-api.quest-light.com/matching")
# How many of the best candidates a job created from a JD is screened with.
JD_SCREEN_TOP = int(os.getenv("JD_SCREEN_TOP") or 3)

# Enables candidate PII redaction and masked PDF requests when true.
MASKING = os.getenv("MASKING_ENABLED", "false").lower() == "true"

# Max upload size in MB (4 MB default on Vercel due to 4.5 MB body limit; 5 MB locally).
MAX_UPLOAD_MB = float(os.getenv("MAX_UPLOAD_MB") or (4 if ON_VERCEL else 5))
MAX_UPLOAD_BYTES = int(MAX_UPLOAD_MB * 1024 * 1024)
MAX_UPLOAD_LABEL = f"{MAX_UPLOAD_MB:g} MB"

# Public base URL for generated /fill/{token} links (falls back to VERCEL_PROJECT_PRODUCTION_URL or relative paths).
_VERCEL_HOST = os.getenv("VERCEL_PROJECT_PRODUCTION_URL", "").strip()
PUBLIC_BASE_URL = (os.getenv("PUBLIC_BASE_URL", "").strip() or (f"https://{_VERCEL_HOST}" if _VERCEL_HOST else "")).rstrip("/")

# Default SQLite path (/tmp/audit.db on serverless deployments).
DEFAULT_AUDIT_DB = Path("/tmp/audit.db") if ON_VERCEL else ROOT / "data" / "audit.db"
