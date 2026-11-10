from pathlib import Path

import psycopg
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

MIGRATIONS = Path(__file__).resolve().parent / "migrations"


def pool_for(url: str) -> ConnectionPool:
    return ConnectionPool(url, kwargs={"row_factory": dict_row}, min_size=1, max_size=8, open=False)


def migrate(url: str) -> None:
    with psycopg.connect(url) as db:
        db.execute("SELECT pg_advisory_xact_lock(3626815)")
        db.execute("CREATE TABLE IF NOT EXISTS hub_migrations (name text PRIMARY KEY)")
        applied = {row[0] for row in db.execute("SELECT name FROM hub_migrations")}
        for path in sorted(MIGRATIONS.glob("*.sql")):
            if path.name not in applied:
                db.execute(path.read_text(encoding="utf-8"))
                db.execute("INSERT INTO hub_migrations VALUES (%s)", (path.name,))
