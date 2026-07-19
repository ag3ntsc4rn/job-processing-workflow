"""Postgres-backed ``Store``.

Implements the two operations this API needs (``enqueue``, ``get_job``) against
the shared ``jobs``/``outbox`` schema. Enqueue writes the job and its outbox row
in one transaction (the transactional-outbox pattern) so the downstream
dispatcher/worker pick the job up reliably. Dedup is enforced by the partial
unique index ``jobs_one_active_per_type`` in the database, not in application
code.

This module talks to a real Postgres and is therefore exercised via
integration/e2e rather than unit tests, so it is excluded from unit coverage.
"""

from __future__ import annotations

import time

from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool

from domain.models import Creator, Job, build_envelope


class PostgresStore:
    def __init__(self, database_url: str, *, max_retries: int = 30) -> None:
        self._pool = ConnectionPool(database_url, min_size=1, max_size=10, open=False)
        self._connect_with_retry(max_retries)

    def _connect_with_retry(self, max_retries: int) -> None:
        last_err: Exception | None = None
        for _ in range(max_retries):
            try:
                self._pool.open(wait=True, timeout=5)
                return
            except Exception as err:  # noqa: BLE001 - retry until Postgres is ready
                last_err = err
                time.sleep(2)
        raise RuntimeError(f"could not connect to Postgres: {last_err}")

    def close(self) -> None:
        self._pool.close()

    def enqueue(
        self,
        job_type: str,
        input_payload: dict | None = None,
        creator: Creator | None = None,
    ) -> int | None:
        creator = creator or Creator()
        with self._pool.connection() as conn:
            with conn.transaction():
                row = conn.execute(
                    """
                    INSERT INTO jobs
                        (job_type, input_payload,
                         created_by_sub, created_by_type, created_by_client)
                    VALUES (%s, %s, %s, %s, %s)
                    ON CONFLICT DO NOTHING
                    RETURNING id
                    """,
                    (
                        job_type,
                        Jsonb(input_payload or {}),
                        creator.sub,
                        creator.type,
                        creator.client_id,
                    ),
                ).fetchone()
                if row is None:
                    return None  # an active job of this type already exists
                job_id = int(row[0])
                conn.execute(
                    "INSERT INTO outbox (job_id, payload) VALUES (%s, %s)",
                    (job_id, Jsonb(build_envelope(job_id, job_type))),
                )
                return job_id

    def get_job(self, job_id: int) -> Job | None:
        with self._pool.connection() as conn:
            row = conn.execute(
                """
                SELECT id, job_type, status, input_payload, payload, attempts,
                       created_at, updated_at,
                       created_by_sub, created_by_type, created_by_client
                FROM jobs WHERE id = %s
                """,
                (job_id,),
            ).fetchone()
        if row is None:
            return None
        return Job(
            id=int(row[0]),
            job_type=row[1],
            status=row[2],
            input_payload=row[3],
            payload=row[4],
            attempts=int(row[5]),
            created_at=row[6],
            updated_at=row[7],
            created_by=Creator(sub=row[8], type=row[9], client_id=row[10]),
        )
