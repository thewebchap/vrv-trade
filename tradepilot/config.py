"""Runtime configuration, read from environment variables.

<project root>/.env is loaded when this module is first imported, before anything reads a variable.
Real environment variables take precedence over .env. Relative paths resolve against the project root,
so TradePilot behaves the same whichever directory it is started from.
"""
import os
from dataclasses import dataclass

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def load_env(path: str | None = None) -> None:
    """KEY=VALUE lines from .env into the environment. Empty values are skipped (the default applies)."""
    path = path or os.path.join(PROJECT_ROOT, ".env")
    if not os.path.exists(path):
        return
    with open(path) as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, value = line.split("=", 1)
                value = value.strip().strip('"').strip("'")
                if value:
                    os.environ.setdefault(key.strip(), value)


load_env()


def env(name: str, default: str) -> str:
    """The variable's value, or the default when it is unset or blank."""
    return (os.getenv(name) or "").strip() or default


def project_path(path: str) -> str:
    """Absolute path; relative paths are taken from the project root."""
    path = os.path.expanduser(path)
    return path if os.path.isabs(path) else os.path.join(PROJECT_ROOT, path)


@dataclass(frozen=True)
class Config:
    model_provider: str = env("LOCAL_MODEL_PROVIDER", "ollama")
    model_name: str = env("LOCAL_MODEL_NAME", "qwen3-vl:8b-instruct")
    ollama_base_url: str = env("OLLAMA_BASE_URL", "http://localhost:11434")
    model_timeout_s: int = int(env("LOCAL_MODEL_TIMEOUT", "300"))
    model_num_ctx: int = int(env("LOCAL_MODEL_NUM_CTX", "8192"))
    ocr_dpi: int = int(env("TRADEPILOT_OCR_DPI", "300"))
    cache_dir: str = project_path(env("TRADEPILOT_CACHE_DIR", ".tradepilot_cache"))
    # The trading company whose payments are being verified. Used to tell
    # purchase-side documents (company is buyer) from sales-side ones (company is seller).
    own_company: str = env("TRADEPILOT_OWN_COMPANY", "VRV GLOBAL")
    checklist_path: str = project_path(env("TRADEPILOT_CHECKLIST", "Documents/Basic Checklist.xlsx"))


CONFIG = Config()


# --------------------------------------------------------------------------- trade storage & Outlook
# Read at call time (not import time) so tests and runs can point them at other locations.

def trade_storage_path() -> str:
    return project_path(env("TRADE_STORAGE_PATH", "trade"))


def allowed_sender_domain() -> str:
    return env("ALLOWED_SENDER_DOMAIN", "vrvglobal.com").lower()


def poll_interval_seconds() -> int:
    return int(env("POLL_INTERVAL_SECONDS", "30"))


def state_db_path() -> str:
    return project_path(env("SQLITE_PATH", os.path.join(trade_storage_path(), "tradepilot_state.db")))


def token_cache_path() -> str:
    return project_path(env("MICROSOFT_TOKEN_CACHE", ".token_cache.json"))


def max_pdf_bytes() -> int:
    return int(env("MAX_PDF_MB", "25")) * 1024 * 1024


def log_level() -> str:
    return env("LOG_LEVEL", "INFO").upper()
