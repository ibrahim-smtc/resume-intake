"""Application configuration loaded from environment variables and local .env files."""
import os
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
if not os.getenv("SKIP_DOTENV"):  # Allows isolated runs to skip loading local .env
    load_dotenv(ROOT / ".env")

STATIC_DIR = ROOT / "app" / "static"

# Resumes and JDs are read by Perfox's extract_structured (app/parsing/service.py).
PARSER = "perfox"

# Perfox API credentials
PERFOX_BASE_URL = os.getenv("PERFOX_BASE_URL", "").rstrip("/")
PERFOX_API_KEY = os.getenv("PERFOX_API_KEY", "")
PERFOX_EXTRACT_MODEL = os.getenv("PERFOX_EXTRACT_MODEL", "").strip()
# WhatsApp notification settings for completed fill-in forms (PERFOX_MESSAGING_KEY overrides PERFOX_API_KEY).
PERFOX_MESSAGING_KEY = os.getenv("PERFOX_MESSAGING_KEY", "")
PERFOX_WHATSAPP_FROM = os.getenv("PERFOX_WHATSAPP_FROM", "").strip()

# Questlight's matching service (its own semantic matching), started for each job created from a JD.
MATCHING_BASE_URL = os.getenv("MATCHING_BASE_URL", "https://dev-api.quest-light.com/matching")
# How many of the best candidates a job created from a JD is screened with.
JD_SCREEN_TOP = int(os.getenv("JD_SCREEN_TOP") or 3)

# Enables candidate PII redaction and masked PDF requests when true.
MASKING = os.getenv("MASKING_ENABLED", "false").lower() == "true"

# Max upload size in MB.
MAX_UPLOAD_MB = float(os.getenv("MAX_UPLOAD_MB") or 5)
MAX_UPLOAD_BYTES = int(MAX_UPLOAD_MB * 1024 * 1024)
MAX_UPLOAD_LABEL = f"{MAX_UPLOAD_MB:g} MB"

# Public base URL for generated /fill/{token} links (relative paths when not set).
PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL", "").strip().rstrip("/")

# MongoDB holds the audit log, the step traces and the Perfox usage. Everything about the deployment (hosts, replica set, SRV,
# credentials, TLS, pool size, timeouts) goes in the connection string, so scaling up is a config change.
MONGODB_URI = os.getenv("MONGODB_URI") or "mongodb://localhost:27017"
# Database: MONGODB_DB, else the database named in the URI, else this default.
MONGODB_DB = os.getenv("MONGODB_DB", "").strip()
DEFAULT_MONGODB_DB = "questlight_local"
# Collection names (overridable, so several environments can share one cluster).
AUDIT_COLLECTION = os.getenv("MONGODB_AUDIT_COLLECTION") or "audit_log"
TRACE_COLLECTION = os.getenv("MONGODB_TRACE_COLLECTION") or "trace_spans"
USAGE_EVENTS_COLLECTION = os.getenv("MONGODB_USAGE_EVENTS_COLLECTION") or "llm_perfox_usage_events"
USAGE_DAILY_COLLECTION = os.getenv("MONGODB_USAGE_DAILY_COLLECTION") or "llm_perfox_usage_daily"
