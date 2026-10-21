"""Apply db/migrations/V*.sql that have not run yet, in version order.

The local docker-compose database applies them once at creation; a managed
database (Cloud SQL) has no such hook, so production runs this instead:

    python -m scripts.migrate
"""
import re
from pathlib import Path

import psycopg

from app import config

MIGRATIONS = Path(__file__).resolve().parent.parent / "db" / "migrations"


def version(path: Path) -> int:
    return int(re.match(r"V(\d+)__", path.name).group(1))


def main() -> None:
    files = sorted(MIGRATIONS.glob("V*__*.sql"), key=version)
    with psycopg.connect(config.DATABASE_URL, autocommit=True) as conn:
        conn.execute("CREATE TABLE IF NOT EXISTS schema_migrations ("
                     "version int PRIMARY KEY, file_name text NOT NULL, applied_at timestamptz NOT NULL DEFAULT now())")
        done = {row[0] for row in conn.execute("SELECT version FROM schema_migrations")}
        for path in files:
            if version(path) in done:
                continue
            with conn.transaction():  # a failed file leaves nothing half-applied
                conn.execute(path.read_text())
                conn.execute("INSERT INTO schema_migrations (version, file_name) VALUES (%s, %s)",
                             [version(path), path.name])
            print(f"applied {path.name}")
    backfill_coordinates()
    # LangGraph's checkpoint tables for the draft revision graph (its own migrations, idempotent).
    from app import revisions
    revisions.ensure_setup()
    print("database is up to date")


def backfill_coordinates() -> None:
    """Published listings saved without a map pin get their town's centre, so the map shows them."""
    from app import geo
    with psycopg.connect(config.DATABASE_URL) as conn:
        rows = conn.execute("SELECT listing_id, location, title FROM listings "
                            "WHERE status = 'published' AND latitude IS NULL").fetchall()
        for listing_id, location, title in rows:
            found = geo.locate(location, title)
            if found:
                conn.execute("UPDATE listings SET latitude = %s, longitude = %s WHERE listing_id = %s",
                             [found[1], found[2], listing_id])
                print(f"map pin set for {listing_id} ({location})")


if __name__ == "__main__":
    main()
