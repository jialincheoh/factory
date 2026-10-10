"""App tests run against a throwaway database, listing_ads_test, on the same
Postgres server (docker compose). The schema is rebuilt from the migrations
for every test session, and the language model is the offline stand-in."""
import os
import re
import tempfile
from pathlib import Path

import psycopg
import pytest

ROOT = Path(__file__).resolve().parent.parent
os.environ.setdefault("POSTGRES_PORT", "5432")
_base = "postgresql://{u}:{p}@localhost:{port}".format(
    u=os.environ.get("POSTGRES_USER", "listing"), p=os.environ.get("POSTGRES_PASSWORD", "listing-local-only"),
    port=os.environ["POSTGRES_PORT"])
os.environ["DATABASE_URL"] = f"{_base}/listing_ads_test"
os.environ["LLM_MODEL"] = "offline"
os.environ["SECURE_COOKIES"] = "0"  # the test client speaks plain http
os.environ["SESSION_SECRET"] = "test-only"
os.environ["UPLOAD_DIR"] = tempfile.mkdtemp(prefix="listing-uploads-")


@pytest.fixture(scope="session", autouse=True)
def database():
    with psycopg.connect(f"{_base}/postgres", autocommit=True) as admin:
        admin.execute("DROP DATABASE IF EXISTS listing_ads_test")
        admin.execute("CREATE DATABASE listing_ads_test")
    migrations = sorted(ROOT.glob("db/migrations/V*__*.sql"), key=lambda p: int(re.match(r"V(\d+)", p.name)[1]))
    with psycopg.connect(os.environ["DATABASE_URL"]) as conn:
        for m in migrations:
            conn.execute(m.read_text())
    yield
