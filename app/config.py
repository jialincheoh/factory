"""Settings, read once from the environment (and .env, if present)."""
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _load_dotenv(path: Path) -> None:
    """Minimal .env reader so the app needs no extra dependency for it.
    Values already set in the real environment win."""
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


_load_dotenv(ROOT / ".env")

DATABASE_URL = os.environ.get(
    "DATABASE_URL",
    "postgresql://{u}:{p}@localhost:{port}/{d}".format(
        port=os.environ.get("POSTGRES_PORT", "5432"),
        u=os.environ.get("POSTGRES_USER", "listing"),
        p=os.environ.get("POSTGRES_PASSWORD", "listing-local-only"),
        d=os.environ.get("POSTGRES_DB", "listing_ads"),
    ),
)

# Any model LangChain's init_chat_model understands, as "provider:model".
# Set to "offline" to run without an API key (rule-based stand-in, for
# development and tests only).
LLM_MODEL = os.environ.get("LLM_MODEL", "openai:gpt-4.1-mini")
DRAFTS_PER_REQUEST = int(os.environ.get("DRAFTS_PER_REQUEST", "3"))

SESSION_SECRET = os.environ.get("SESSION_SECRET", "")
# Cookies are marked Secure unless this is "0" (plain-http local development).
SECURE_COOKIES = os.environ.get("SECURE_COOKIES", "1") != "0"

UPLOAD_DIR = Path(os.environ.get("UPLOAD_DIR", ROOT / "uploads"))
MAX_IMAGES = 8
MAX_IMAGE_BYTES = 10 * 1024 * 1024
CURRENCY = os.environ.get("CURRENCY", "RM")
CURRENCY_CODE = os.environ.get("CURRENCY_CODE", "MYR")  # ISO 4217, for structured data

# Public address of the site, used in canonical links, the sitemap and
# structured data. Set it to the real domain (e.g. https://factory.my) when deploying.
SITE_URL = os.environ.get("SITE_URL", "http://localhost:8000").rstrip("/")
SITE_NAME = os.environ.get("SITE_NAME", "Factory Finder")
