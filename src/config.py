"""Application settings, read from the environment.

A frozen dataclass so there's no extra dependency. The API gateway (Apigee) owns
edge concerns — rate limiting, CORS, TLS — so none of those live here.

Two knobs matter:

* ``database_url`` is **optional**. Unset -> the app runs on the process-local
  :class:`~store.memory.InMemoryStore` (demo / no infra). Set -> the real
  :class:`~store.postgres.PostgresStore` takes over with no code change.
* ``auth_verify`` selects the auth mode:
    - **on (default / prod)**: the token must be minted by the configured issuer
      (Apigee / IdP) and the app re-verifies signature + ``iss`` / ``aud`` /
      ``exp`` via JWKS. Requires ``OIDC_ISSUER`` / ``OIDC_AUDIENCE`` and either
      ``OIDC_JWKS_URL`` or issuer discovery.
    - **off (dev)**: no signature check — claims are read straight from the token
      payload, so a developer can craft their own token with any scopes. Never
      use outside local development.

Scope *names* are env-overridable so ops can align them to whatever the IdP
mints; which endpoint requires which scope is policy and lives in code
(``api/deps``).
"""

from __future__ import annotations

import os
from dataclasses import dataclass


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    # None -> in-memory store (demo); set -> PostgresStore takes over.
    database_url: str | None

    # True (default) -> re-verify JWT signature/iss/aud/exp against JWKS (prod).
    # False -> dev mode: read claims from the token payload without verifying.
    auth_verify: bool = True

    # JWT resource-server config (only needed when auth_verify is on): issuer /
    # jwks / audience of whoever signs the token this service receives.
    oidc_issuer: str = ""
    oidc_audience: str = ""
    oidc_jwks_url: str = ""  # empty -> derived from issuer discovery at startup
    oidc_algorithms: tuple[str, ...] = ("RS256",)
    clock_skew_leeway: int = 60  # seconds tolerated on exp/nbf/iat
    jwks_cache_ttl: float = 3600.0

    # Authorization scopes (standard OAuth2 space-delimited `scope` claim).
    scope_write: str = "jobs.write"
    scope_read: str = "jobs.read"

    # transport
    host: str = "0.0.0.0"  # noqa: S104 - bind all inside the container
    port: int = 8080

    app_name: str = "job-api"
    app_version: str = "0.1.0"

    @classmethod
    def from_env(cls) -> Settings:
        return cls(
            database_url=os.getenv("DATABASE_URL") or None,
            auth_verify=_env_bool("AUTH_VERIFY", True),
            oidc_issuer=os.getenv("OIDC_ISSUER", ""),
            oidc_audience=os.getenv("OIDC_AUDIENCE", ""),
            oidc_jwks_url=os.getenv("OIDC_JWKS_URL", ""),
            clock_skew_leeway=int(os.getenv("OIDC_CLOCK_SKEW_LEEWAY", "60")),
            jwks_cache_ttl=float(os.getenv("OIDC_JWKS_CACHE_TTL", "3600")),
            scope_write=os.getenv("SCOPE_WRITE", "jobs.write"),
            scope_read=os.getenv("SCOPE_READ", "jobs.read"),
            host=os.getenv("HOST", "0.0.0.0"),  # noqa: S104
            port=int(os.getenv("PORT", "8080")),
        )
