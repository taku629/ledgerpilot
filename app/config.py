"""Runtime configuration — everything sensitive comes from env vars only."""
import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = Path(os.environ.get("LEDGERPILOT_DATA_DIR", BASE_DIR / "data"))
AUDIT_LOG = Path(os.environ.get("LEDGERPILOT_AUDIT_LOG", DATA_DIR / "audit_log.jsonl"))

# --- LLM (OpenAI-compatible). App works without these via the deterministic agent. ---
LLM_BASE_URL = os.environ.get("LLM_BASE_URL", "")
LLM_API_KEY = os.environ.get("LLM_API_KEY", "")
LLM_MODEL = os.environ.get("LLM_MODEL", "")

# --- Security policy ---
CURRENCY = os.environ.get("LEDGERPILOT_CURRENCY", "USD")
# Tier-2 sandbox actions above this single-action amount are refused.
MAX_SANDBOX_ACTION_AMOUNT = float(os.environ.get("LEDGERPILOT_MAX_ACTION_AMOUNT", "50000"))
# Approval tokens expire after this many seconds.
APPROVAL_TTL_SECONDS = int(os.environ.get("LEDGERPILOT_APPROVAL_TTL", "300"))
# Minimum cash buffer used for "balance below threshold" alerts.
DEFAULT_CASH_BUFFER = float(os.environ.get("LEDGERPILOT_CASH_BUFFER", "15000"))

DEMO_BUSINESS_NAME = os.environ.get("LEDGERPILOT_BUSINESS", "Harbor & Bean Roasters")

DISCLAIMER = (
    "LedgerPilot provides informational analysis only. It is not financial, investment, "
    "tax, or legal advice. Final financing and payment decisions remain with the owner."
)
