"""API settings, read from the environment.

Frozen dataclass (matching ``common.config`` / ``handlerAPI.config``) so there's
no extra dependency. The gateway (Apigee) owns edge concerns — rate limiting,
CORS, TLS — so none of those live here; this service only validates a JWT and
enqueues.

Two knobs differ from ``handlerAPI``:

* ``database_url`` is **optional**. Unset -> the app runs on the process-local
  :class:`~common.store.InMemoryStore` (demo / no infra). Set -> the real
  :class:`~common.db.PostgresStore` takes over with no code change.
* the OIDC ``issuer`` / ``jwks_url`` / ``audience`` point at whoever signs the
  token the service actually receives (Keycloak in local dev, Apigee in prod).
"""

from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Settings:
    # None -> in-memory store (demo); set -> PostgresStore takes over.
    database_url: str | None

    # JWT resource-server config (issuer/jwks/audience of whoever signs the
    # token this service receives — Keycloak in dev, Apigee in prod).
    oidc_issuer: str
    oidc_audience: str
    oidc_jwks_url: str  # empty -> derived from issuer discovery at startup
    oidc_algorithms: tuple[str, ...] = ("RS256",)
    clock_skew_leeway: int = 60  # seconds tolerated on exp/nbf/iat
    jwks_cache_ttl: float = 3600.0

    # authorization scopes (standard OAuth2 space-delimited `scope` claim)
    scope_write: str = "jobs.write"
    scope_read: str = "jobs.read"

    # Circuit breaker around the Postgres store: trip after this many consecutive
    # failures, then fail fast for this many seconds before a half-open trial.
    db_circuit_failure_threshold: int = 5
    db_circuit_reset_timeout: float = 30.0

    # transport
    host: str = "0.0.0.0"  # noqa: S104 - bind all inside the container
    port: int = 8080

    app_name: str = "job-handler-api-v2"
    app_version: str = "0.1.0"

    @classmethod
    def from_env(cls) -> Settings:
        issuer = os.getenv("OIDC_ISSUER", "http://keycloak:8080/realms/jobs")
        return cls(
            database_url=os.getenv("DATABASE_URL") or None,
            oidc_issuer=issuer,
            oidc_audience=os.getenv("OIDC_AUDIENCE", "job-api"),
            oidc_jwks_url=os.getenv("OIDC_JWKS_URL", ""),
            clock_skew_leeway=int(os.getenv("OIDC_CLOCK_SKEW_LEEWAY", "60")),
            jwks_cache_ttl=float(os.getenv("OIDC_JWKS_CACHE_TTL", "3600")),
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
