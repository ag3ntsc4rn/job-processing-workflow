from __future__ import annotations

import pytest

from jobclient.config import Config, ConfigError


def test_defaults_target_localhost_and_write_scope() -> None:
    config = Config.from_env()
    assert config.jobs_url == "http://localhost:8080/v1/jobs"
    assert config.scopes == ("jobs.write",)
    assert config.max_attempts == 4


def test_env_overrides(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JOB_API_URL", "https://jobs.example.com/")
    monkeypatch.setenv("JOB_API_JOBS_PATH", "/v2/jobs")
    monkeypatch.setenv("JOB_API_SCOPES", "jobs.write jobs.read")
    monkeypatch.setenv("JOB_API_MAX_ATTEMPTS", "7")
    monkeypatch.setenv("JOB_API_TIMEOUT", "2.5")
    monkeypatch.setenv("JOB_API_TOKEN_TTL", "60")
    config = Config.from_env()
    assert config.jobs_url == "https://jobs.example.com/v2/jobs"
    assert config.scopes == ("jobs.write", "jobs.read")
    assert (config.max_attempts, config.timeout_seconds, config.jwt_ttl_seconds) == (7, 2.5, 60)


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("JOB_API_TIMEOUT", "soon"),
        ("JOB_API_MAX_ATTEMPTS", "many"),
        ("JOB_API_MAX_ATTEMPTS", "0"),
        ("JOB_API_SCOPES", "   "),
    ],
)
def test_unusable_env_values_are_rejected(
    monkeypatch: pytest.MonkeyPatch, name: str, value: str
) -> None:
    monkeypatch.setenv(name, value)
    with pytest.raises(ConfigError):
        Config.from_env()
