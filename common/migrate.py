"""Apply the SQL migrations in ``migrations/`` (idempotent).

Run as ``python -m common.migrate``. The compose stack runs this once before
starting the services so every component boots against a ready schema.
"""

from __future__ import annotations

import pathlib
import sys

import psycopg

from common.config import Config

MIGRATIONS_DIR = pathlib.Path(__file__).resolve().parent.parent / "migrations"


def run(database_url: str) -> None:
    files = sorted(MIGRATIONS_DIR.glob("*.sql"))
    with psycopg.connect(database_url) as conn:
        for path in files:
            sys.stdout.write(f"applying {path.name}\n")
            conn.execute(path.read_text())
        conn.commit()


def main() -> None:
    run(Config.from_env().database_url)
    sys.stdout.write("migrations complete\n")


if __name__ == "__main__":
    main()
