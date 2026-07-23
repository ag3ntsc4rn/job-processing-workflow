"""Application settings, read from the environment.

A frozen dataclass so there's no extra dependency. The API gateway (Apigee) owns
edge concerns — rate limiting, CORS, TLS — and the enterprise JWT auth middleware
owns token validation (signature / issuer / audience / expiry), so none of those
live here. This service only reads validated claims, enforces scopes, and
enqueues.

One knob matters:

* ``database_url`` is **optional**. Unset -> the app runs on the process-local
  :class:`~store.memory.InMemoryStore` (demo / no infra). Set -> the real
  :class:`~store.postgres.PostgresStore` takes over with no code change.

Scope *names* are env-overridable so ops can align them to whatever the IdP
mints; which endpoint requires which scope is policy and lives in code
(``api/deps``). No OIDC/JWKS config is needed — authentication is upstream.
"""

from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Settings:
    # None -> in-memory store (demo); set -> PostgresStore takes over.
    database_url: str | None

    # Authorization scopes (standard OAuth2 space-delimited `scope` claim).
    scope_write: str = "jobs.write"
    scope_read: str = "jobs.read"

    # Circuit breaker around the Postgres store: trip after this many consecutive
    # failures, then fail fast for this many seconds before a half-open trial.
    db_circuit_failure_threshold: int = 5
    db_circuit_reset_timeout: float = 30.0

    # transport
    host: str = "0.0.0.0"  # noqa: S104 - bind all inside the container
    port: int = 8080

    app_name: str = "job-api"
    app_version: str = "0.1.0"

    @classmethod
    def from_env(cls) -> Settings:
        return cls(
            database_url=os.getenv("DATABASE_URL") or None,
            scope_write=os.getenv("SCOPE_WRITE", "jobs.write"),
            scope_read=os.getenv("SCOPE_READ", "jobs.read"),
            db_circuit_failure_threshold=int(
                os.getenv("DB_CIRCUIT_FAILURE_THRESHOLD", "5")
            ),
            db_circuit_reset_timeout=float(
                os.getenv("DB_CIRCUIT_RESET_TIMEOUT", "30")
            ),
            host=os.getenv("HOST", "0.0.0.0"),  # noqa: S104
            port=int(os.getenv("PORT", "8080")),
        )
