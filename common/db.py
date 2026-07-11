"""Postgres-backed ``Store``.

Implements the same operations as ``InMemoryStore`` against a real database,
using the schema in ``migrations/``. Correctness rests on constraints and
guarded (compare-and-set) updates rather than application-side checks:

* dedup is the partial unique index ``jobs_one_active_per_type``;
* the worker claim is ``UPDATE ... WHERE status='dispatched'``;
* the reaper re-queue is ``UPDATE ... WHERE status='running' AND attempts < cap``.

Per-type ``run_timeout`` / ``max_attempts`` come from ``job_type_config`` and
fall back to defaults when a type has no row. This module talks to a real
Postgres and is exercised by docker-compose / e2e, so it is excluded from unit
coverage.
"""

from __future__ import annotations

import time

from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool

from common.models import Job, OutboxMessage, StuckJob, build_envelope

_DEFAULT_RUN_TIMEOUT = "15 minutes"
_DEFAULT_MAX_ATTEMPTS = 3


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

    # -- handler -----------------------------------------------------------
    def enqueue(self, job_type: str) -> int | None:
        with self._pool.connection() as conn:
            with conn.transaction():
                row = conn.execute(
                    """
                    INSERT INTO jobs (job_type)
                    VALUES (%s)
                    ON CONFLICT DO NOTHING
                    RETURNING id
                    """,
                    (job_type,),
                ).fetchone()
                if row is None:
                    return None  # an active job of this type already exists
                job_id = int(row[0])
                conn.execute(
                    "INSERT INTO outbox (job_id, payload) VALUES (%s, %s)",
                    (job_id, Jsonb(build_envelope(job_id, job_type))),
                )
                return job_id

    # -- dispatcher --------------------------------------------------------
    def fetch_unpublished(self, limit: int) -> list[OutboxMessage]:
        with self._pool.connection() as conn:
            rows = conn.execute(
                """
                SELECT id, job_id, payload FROM outbox
                WHERE published_at IS NULL
                ORDER BY id
                FOR UPDATE SKIP LOCKED
                LIMIT %s
                """,
                (limit,),
            ).fetchall()
        return [OutboxMessage(id=int(r[0]), job_id=int(r[1]), payload=r[2]) for r in rows]

    def mark_dispatched(self, outbox_id: int, job_id: int) -> None:
        with self._pool.connection() as conn:
            with conn.transaction():
                conn.execute(
                    "UPDATE outbox SET published_at = now() WHERE id = %s", (outbox_id,)
                )
                conn.execute(
                    """
                    UPDATE jobs SET status = 'dispatched', updated_at = now()
                    WHERE id = %s AND status = 'queued'
                    """,
                    (job_id,),
                )

    # -- worker ------------------------------------------------------------
    def claim(self, job_id: int) -> bool:
        # Accept queued OR dispatched: receiving the message already proves it
        # was published, and a fast worker can outrace the dispatcher's
        # publish -> mark-dispatched step, seeing the row still 'queued'.
        return self._guarded_update(
            "UPDATE jobs SET status='running', updated_at=now() "
            "WHERE id=%s AND status IN ('queued', 'dispatched')",
            job_id,
        )

    def snapshot_config_payload(self, job_id: int, job_type: str) -> dict | None:
        # Copy the type's configured payload into the claimed run, recording the
        # exact inputs it ran with. Joins job_type_config, so a type with no
        # config row yields no updated row -> None -> the worker fails it.
        with self._pool.connection() as conn:
            row = conn.execute(
                """
                UPDATE jobs j
                SET payload = c.payload, updated_at = now()
                FROM job_type_config c
                WHERE j.id = %s AND c.job_type = %s AND j.status = 'running'
                RETURNING j.payload
                """,
                (job_id, job_type),
            ).fetchone()
        return row[0] if row is not None else None

    def complete(self, job_id: int) -> bool:
        return self._guarded_update(
            "UPDATE jobs SET status='completed', updated_at=now() "
            "WHERE id=%s AND status='running'",
            job_id,
        )

    def fail(self, job_id: int) -> bool:
        return self._guarded_update(
            "UPDATE jobs SET status='failed', updated_at=now() "
            "WHERE id=%s AND status='running'",
            job_id,
        )

    def _guarded_update(self, sql: str, job_id: int) -> bool:
        with self._pool.connection() as conn:
            cur = conn.execute(sql, (job_id,))
            return cur.rowcount == 1

    # -- reaper ------------------------------------------------------------
    def find_stuck(self) -> list[StuckJob]:
        with self._pool.connection() as conn:
            rows = conn.execute(
                """
                SELECT j.id, j.job_type, j.attempts
                FROM jobs j
                LEFT JOIN job_type_config c USING (job_type)
                WHERE j.status = 'running'
                  AND j.updated_at < now() - COALESCE(c.run_timeout, %s::interval)
                """,
                (_DEFAULT_RUN_TIMEOUT,),
            ).fetchall()
        return [StuckJob(job_id=int(r[0]), job_type=r[1], attempts=int(r[2])) for r in rows]

    def requeue_stuck(self, job_id: int) -> bool:
        with self._pool.connection() as conn:
            with conn.transaction():
                cur = conn.execute(
                    """
                    UPDATE jobs SET status='queued', attempts=attempts+1, updated_at=now()
                    WHERE id=%s AND status='running'
                      AND attempts < COALESCE(
                          (SELECT max_attempts FROM job_type_config c
                           WHERE c.job_type = jobs.job_type),
                          %s
                      )
                    """,
                    (job_id, _DEFAULT_MAX_ATTEMPTS),
                )
                if cur.rowcount != 1:
                    return False
                # re-arm: make this job's outbox message publishable again
                conn.execute(
                    "UPDATE outbox SET published_at = NULL WHERE job_id = %s", (job_id,)
                )
                return True

    def dead_letter(self, job_id: int) -> bool:
        return self._guarded_update(
            "UPDATE jobs SET status='failed', updated_at=now() "
            "WHERE id=%s AND status='running'",
            job_id,
        )

    # -- read --------------------------------------------------------------
    def get_job(self, job_id: int) -> Job | None:
        with self._pool.connection() as conn:
            row = conn.execute(
                """
                SELECT id, job_type, status, payload, attempts, created_at, updated_at
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
            payload=row[3],
            attempts=int(row[4]),
            created_at=row[5],
            updated_at=row[6],
        )
