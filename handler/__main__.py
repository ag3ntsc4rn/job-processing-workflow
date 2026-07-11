"""CLI entrypoint standing in for an AutoSys trigger.

    python -m handler <job_type>

Enqueues one job (or reports that an active one already exists). The producer
only names the job_type — the business payload lives in ``job_type_config`` and
is snapshotted by the worker. Any producer that can call
``handler.service.enqueue`` against the store is equivalent — the CLI is just
the demo's stand-in for AutoSys.
"""

from __future__ import annotations

import sys

from common.config import Config
from common.db import PostgresStore
from handler.service import enqueue


def main(argv: list[str]) -> int:
    if not argv:
        sys.stderr.write("usage: python -m handler <job_type>\n")
        return 2
    job_type = argv[0]

    store = PostgresStore(Config.from_env().database_url)
    try:
        result = enqueue(store, job_type)
    finally:
        store.close()

    if result.enqueued:
        sys.stdout.write(f"enqueued {job_type} as job {result.job_id}\n")
    else:
        sys.stdout.write(f"skipped {job_type}: an active job already exists\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
