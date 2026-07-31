"""Configuration for the enqueue client, resolved from environment variables."""

from __future__ import annotations

import os
from dataclasses import dataclass, field

DEFAULT_API_URL = "http://localhost:8080"
DEFAULT_JOBS_PATH = "/v1/jobs"
DEFAULT_SCOPES = ("jobs.write",)
# Placeholder HMAC key: nothing verifies these tokens yet, and it is long enough
# to satisfy RFC 7518 §3.2 so PyJWT does not warn.
DEFAULT_JWT_SECRET = "unverified-placeholder-secret-for-local-use-only"
RETRY_STATUS_CODES = frozenset({408, 425, 429, 500, 502, 503, 504})


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} must be a number, got {raw!r}") from exc


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} must be an integer, got {raw!r}") from exc


class ConfigError(ValueError):
    """Raised when the environment holds an unusable value."""


@dataclass(frozen=True)
class Config:
    """Everything the client needs: where the API is, and how to mint a token.

    The API does not verify the token today (it sits behind a gateway that will
    own verification later), so the signing material here is only a placeholder
    that keeps the wire format honest: a real, decodable JWT carrying the scopes
    the caller claims. Swapping in an OAuth2 client-credentials token means
    replacing ``jobclient.tokens.mint_jwt`` at its single call site.
    """

    api_url: str = DEFAULT_API_URL
    jobs_path: str = DEFAULT_JOBS_PATH
    scopes: tuple[str, ...] = DEFAULT_SCOPES
    issuer: str = "job-enqueue-client"
    audience: str = "job-api"
    subject: str = "job-enqueue-client"
    jwt_algorithm: str = "HS256"
    jwt_secret: str = DEFAULT_JWT_SECRET
    jwt_ttl_seconds: int = 300
    timeout_seconds: float = 10.0
    max_attempts: int = 4
    backoff_initial_seconds: float = 0.5
    backoff_max_seconds: float = 8.0
    retry_status_codes: frozenset[int] = field(default_factory=lambda: RETRY_STATUS_CODES)

    @property
    def jobs_url(self) -> str:
        return f"{self.api_url.rstrip('/')}{self.jobs_path}"

    @classmethod
    def from_env(cls) -> Config:
        scopes = tuple(os.environ.get("JOB_API_SCOPES", " ".join(DEFAULT_SCOPES)).split())
        if not scopes:
            raise ConfigError("JOB_API_SCOPES must name at least one scope")
        max_attempts = _env_int("JOB_API_MAX_ATTEMPTS", 4)
        if max_attempts < 1:
            raise ConfigError("JOB_API_MAX_ATTEMPTS must be >= 1")
        return cls(
            api_url=os.environ.get("JOB_API_URL", DEFAULT_API_URL),
            jobs_path=os.environ.get("JOB_API_JOBS_PATH", DEFAULT_JOBS_PATH),
            scopes=scopes,
            issuer=os.environ.get("JOB_API_TOKEN_ISSUER", "job-enqueue-client"),
            audience=os.environ.get("JOB_API_TOKEN_AUDIENCE", "job-api"),
            subject=os.environ.get("JOB_API_TOKEN_SUBJECT", "job-enqueue-client"),
            jwt_algorithm=os.environ.get("JOB_API_TOKEN_ALGORITHM", "HS256"),
            jwt_secret=os.environ.get("JOB_API_TOKEN_SECRET", DEFAULT_JWT_SECRET),
            jwt_ttl_seconds=_env_int("JOB_API_TOKEN_TTL", 300),
            timeout_seconds=_env_float("JOB_API_TIMEOUT", 10.0),
            max_attempts=max_attempts,
            backoff_initial_seconds=_env_float("JOB_API_BACKOFF_INITIAL", 0.5),
            backoff_max_seconds=_env_float("JOB_API_BACKOFF_MAX", 8.0),
        )
