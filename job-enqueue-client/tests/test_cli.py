from __future__ import annotations

import httpx
import pytest
from conftest import make_client

from jobclient import cli
from jobclient.client import EnqueueError, EnqueueResult
from jobclient.config import Config


@pytest.fixture
def calls() -> list[tuple[str, dict, Config]]:
    return []


@pytest.fixture(autouse=True)
def stub_client(
    monkeypatch: pytest.MonkeyPatch, calls: list[tuple[str, dict, Config]]
) -> list[EnqueueResult | Exception]:
    """Record what the CLI asks the client to do; replay a scripted outcome."""
    outcomes: list[EnqueueResult | Exception] = [
        EnqueueResult(status_code=201, attempts=1, body={"job_id": 7})
    ]

    class StubClient:
        def __init__(self, config: Config) -> None:
            self._config = config

        def enqueue(self, job_type: str, payload: dict | None = None) -> EnqueueResult:
            calls.append((job_type, payload or {}, self._config))
            outcome = outcomes[0]
            if isinstance(outcome, Exception):
                raise outcome
            return outcome

    monkeypatch.setattr(cli, "JobClient", StubClient)
    return outcomes


def test_job_type_only(capsys: pytest.CaptureFixture[str], calls: list) -> None:
    assert cli.main(["settlement"]) == 0
    assert calls[0][0:2] == ("settlement", {})
    assert capsys.readouterr().out == "enqueued settlement as job 7 (attempts: 1)\n"


def test_job_type_with_payload(calls: list) -> None:
    assert cli.main(["settlement", '{"region": "eu"}']) == 0
    assert calls[0][1] == {"region": "eu"}


@pytest.mark.parametrize("payload", ["{not json", "[1, 2]", '"eu"'])
def test_bad_payload_is_a_usage_error(
    capsys: pytest.CaptureFixture[str], payload: str, calls: list
) -> None:
    assert cli.main(["settlement", payload]) == 2
    assert "error:" in capsys.readouterr().err
    assert calls == []


def test_flags_override_env(monkeypatch: pytest.MonkeyPatch, calls: list) -> None:
    monkeypatch.setenv("JOB_API_URL", "https://from-env.example.com")
    argv = ["settlement", "--api-url", "https://flag.example.com", "--scope", "jobs.write"]
    assert cli.main([*argv, "--scope", "jobs.read", "--max-attempts", "2"]) == 0
    config = calls[0][2]
    assert config.jobs_url == "https://flag.example.com/v1/jobs"
    assert config.scopes == ("jobs.write", "jobs.read")
    assert config.max_attempts == 2


def test_env_is_used_when_no_flags(monkeypatch: pytest.MonkeyPatch, calls: list) -> None:
    monkeypatch.setenv("JOB_API_URL", "https://from-env.example.com")
    assert cli.main(["settlement"]) == 0
    assert calls[0][2].jobs_url == "https://from-env.example.com/v1/jobs"


def test_zero_max_attempts_is_a_usage_error(capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["settlement", "--max-attempts", "0"]) == 2
    assert "--max-attempts must be >= 1" in capsys.readouterr().err


def test_duplicate_job_exits_zero(
    capsys: pytest.CaptureFixture[str], stub_client: list
) -> None:
    stub_client[0] = EnqueueResult(status_code=409, attempts=1, body={"title": "already active"})
    assert cli.main(["settlement"]) == 0
    assert capsys.readouterr().out == "skipped settlement: an active job already exists\n"


def test_rejected_request_exits_one(
    capsys: pytest.CaptureFixture[str], stub_client: list
) -> None:
    stub_client[0] = EnqueueResult(status_code=403, attempts=1, body={"title": "forbidden"})
    assert cli.main(["settlement"]) == 1
    assert "HTTP 403" in capsys.readouterr().err


def test_exhausted_retries_exit_one(
    capsys: pytest.CaptureFixture[str], stub_client: list
) -> None:
    stub_client[0] = EnqueueError("boom", attempts=4, status_code=503)
    assert cli.main(["settlement"]) == 1
    assert "error: boom" in capsys.readouterr().err


def test_success_without_job_id_still_reports(
    capsys: pytest.CaptureFixture[str], stub_client: list
) -> None:
    stub_client[0] = EnqueueResult(status_code=202, attempts=2, body={})
    assert cli.main(["settlement"]) == 0
    assert capsys.readouterr().out == "enqueued settlement (attempts: 2)\n"


def test_missing_job_type_exits_two() -> None:
    with pytest.raises(SystemExit) as excinfo:
        cli.main([])
    assert excinfo.value.code == 2


def test_end_to_end_against_a_mock_transport(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The real client, wired to an in-process API, through the CLI."""
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(201, json={"job_id": 99})

    monkeypatch.setattr(cli, "JobClient", lambda config: make_client(config, handler))
    assert cli.main(["settlement", '{"region": "eu"}']) == 0
    assert seen[0].headers["Authorization"].startswith("Bearer ")
    assert capsys.readouterr().out == "enqueued settlement as job 99 (attempts: 1)\n"
