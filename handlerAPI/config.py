"""API settings, read from the environment.

Kept as a frozen dataclass to match ``common.config`` (no extra dependency).
Secrets are never defaulted to real values — the OIDC issuer/audience must be
supplied in any real deployment; the defaults only make the local docker-compose
+ mock-OIDC stack work out of the box.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field


def _split(value: str) -> tuple[str, ...]:
    return tuple(v.strip() for v in value.split(",") if v.strip())


@dataclass(frozen=True)
class Settings:
    database_url: str

    # OIDC / Ping Federate resource-server config
    oidc_issuer: str
    oidc_audience: str
    oidc_jwks_url: str  # empty -> derived from issuer discovery at startup
    oidc_algorithms: tuple[str, ...] = ("RS256",)
    clock_skew_leeway: int = 60  # seconds tolerated on exp/nbf/iat
    jwks_cache_ttl: float = 3600.0

    # authorization
    scope_write: str = "jobs.write"
    scope_read: str = "jobs.read"
    scope_read_all: str = "jobs.read.all"  # lets a caller read jobs it didn't create
    # claim that carries AD/role groups (used once Ping's group shape is confirmed)
    groups_claim: str = "groups"
    # presence of any of these claims marks the principal a human 'user'
    # (client-credentials tokens have none) — a heuristic, refined per Ping's tokens
    user_claims: tuple[str, ...] = ("email", "preferred_username", "name")

    # transport / hardening
    cors_allow_origins: tuple[str, ...] = field(default_factory=tuple)
    rate_limit: str = "60/minute"
    rate_limit_enabled: bool = True
    host: str = "0.0.0.0"  # noqa: S104 - bind all inside the container
    port: int = 8080
    tls_certfile: str | None = None
    tls_keyfile: str | None = None

    app_name: str = "job-handler-api"
    app_version: str = "0.1.0"

    @classmethod
    def from_env(cls) -> Settings:
        issuer = os.getenv("OIDC_ISSUER", "http://mock-oidc:8080/default")
        return cls(
            database_url=os.getenv("DATABASE_URL", "postgresql://app:app@localhost:5432/app"),
            oidc_issuer=issuer,
            oidc_audience=os.getenv("OIDC_AUDIENCE", "job-api"),
            oidc_jwks_url=os.getenv("OIDC_JWKS_URL", ""),
            clock_skew_leeway=int(os.getenv("OIDC_CLOCK_SKEW_LEEWAY", "60")),
            jwks_cache_ttl=float(os.getenv("OIDC_JWKS_CACHE_TTL", "3600")),
            scope_write=os.getenv("SCOPE_WRITE", "jobs.write"),
            scope_read=os.getenv("SCOPE_READ", "jobs.read"),
            scope_read_all=os.getenv("SCOPE_READ_ALL", "jobs.read.all"),
            groups_claim=os.getenv("OIDC_GROUPS_CLAIM", "groups"),
            cors_allow_origins=_split(os.getenv("CORS_ALLOW_ORIGINS", "")),
            rate_limit=os.getenv("RATE_LIMIT", "60/minute"),
            rate_limit_enabled=os.getenv("RATE_LIMIT_ENABLED", "true").lower() == "true",
            host=os.getenv("HOST", "0.0.0.0"),  # noqa: S104
            port=int(os.getenv("PORT", "8080")),
            tls_certfile=os.getenv("TLS_CERTFILE") or None,
            tls_keyfile=os.getenv("TLS_KEYFILE") or None,
        )
