from __future__ import annotations

import httpx
import pytest

from jobclient.config import Config

JOB_API_ENV_VARS = (
    "JOB_API_URL",
    "JOB_API_JOBS_PATH",
    "JOB_API_SCOPES",
    "JOB_API_TOKEN_ISSUER",
    "JOB_API_TOKEN_AUDIENCE",
    "JOB_API_TOKEN_SUBJECT",
    "JOB_API_TOKEN_ALGORITHM",
    "JOB_API_TOKEN_SECRET",
    "JOB_API_TOKEN_TTL",
    "JOB_API_TIMEOUT",
    "JOB_API_MAX_ATTEMPTS",
    "JOB_API_BACKOFF_INITIAL",
    "JOB_API_BACKOFF_MAX",
)


@pytest.fixture(autouse=True)
def clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep every test independent of the ambient environment."""
    for name in JOB_API_ENV_VARS:
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def config() -> Config:
    return Config(api_url="https://jobs.example.com", max_attempts=3, backoff_initial_seconds=0.1)


@pytest.fixture
def sleeps() -> list[float]:
    return []


def make_client(config: Config, handler, sleeps: list[float] | None = None):
    """Build a ``JobClient`` wired to an in-process transport and a fake clock."""
    from jobclient.client import JobClient

    recorded = sleeps if sleeps is not None else []
    return JobClient(
        config,
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        sleep=recorded.append,
        jitter=lambda _low, high: high,
    )
