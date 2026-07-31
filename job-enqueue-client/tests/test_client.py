from __future__ import annotations

import json

import httpx
import jwt
import pytest
from conftest import make_client

from jobclient.client import EnqueueError, JobClient
from jobclient.config import Config


def _created(request: httpx.Request) -> httpx.Response:
    return httpx.Response(201, json={"job_id": 42, "job_type": "settlement", "status": "queued"})


def test_posts_job_type_payload_and_bearer_token(config: Config) -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return _created(request)

    result = make_client(config, handler).enqueue("settlement", {"region": "eu"})

    assert (result.status_code, result.job_id, result.attempts) == (201, 42, 1)
    request = seen[0]
    assert str(request.url) == "https://jobs.example.com/v1/jobs"
    assert json.loads(request.content) == {
        "job_type": "settlement",
        "payload": {"region": "eu"},
    }
    scheme, _, token = request.headers["Authorization"].partition(" ")
    assert scheme == "Bearer"
    claims = jwt.decode(token, options={"verify_signature": False})
    assert claims["scope"] == "jobs.write"


def test_omits_payload_key_when_no_payload(config: Config) -> None:
    seen: list[bytes] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.content)
        return _created(request)

    make_client(config, handler).enqueue("settlement")
    assert json.loads(seen[0]) == {"job_type": "settlement"}


def test_empty_job_type_is_rejected_before_any_request(config: Config) -> None:
    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover - never called
        raise AssertionError("no request expected")

    with pytest.raises(ValueError, match="job_type"):
        make_client(config, handler).enqueue("")


def test_duplicate_is_reported_not_retried(config: Config, sleeps: list[float]) -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(409, json={"title": "job already active"})

    result = make_client(config, handler, sleeps).enqueue("settlement")

    assert (result.duplicate, result.status_code, calls, sleeps) == (True, 409, 1, [])


def test_client_error_is_returned_without_retrying(config: Config, sleeps: list[float]) -> None:
    result = make_client(
        config, lambda request: httpx.Response(403, json={"title": "forbidden"}), sleeps
    ).enqueue("settlement")

    assert (result.status_code, result.job_id, sleeps) == (403, None, [])


def test_retries_transient_status_then_succeeds(config: Config, sleeps: list[float]) -> None:
    statuses = [503, 500, 201]

    def handler(request: httpx.Request) -> httpx.Response:
        status = statuses.pop(0)
        return _created(request) if status == 201 else httpx.Response(status, text="nope")

    result = make_client(config, handler, sleeps).enqueue("settlement")

    assert (result.status_code, result.attempts) == (201, 3)
    assert sleeps == [0.1, 0.2]


def test_retries_connection_errors_then_succeeds(config: Config, sleeps: list[float]) -> None:
    attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise httpx.ConnectError("connection refused", request=request)
        return _created(request)

    result = make_client(config, handler, sleeps).enqueue("settlement")

    assert (result.attempts, sleeps) == (2, [0.1])


def test_raises_after_exhausting_attempts(config: Config, sleeps: list[float]) -> None:
    with pytest.raises(EnqueueError) as excinfo:
        make_client(
            config, lambda request: httpx.Response(503, text="unavailable"), sleeps
        ).enqueue("settlement")

    assert excinfo.value.attempts == 3
    assert excinfo.value.status_code == 503
    assert sleeps == [0.1, 0.2]


def test_raises_after_exhausting_attempts_on_timeout(config: Config, sleeps: list[float]) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("too slow", request=request)

    with pytest.raises(EnqueueError) as excinfo:
        make_client(config, handler, sleeps).enqueue("settlement")

    assert excinfo.value.status_code is None
    assert "ReadTimeout" in str(excinfo.value)


@pytest.mark.parametrize(
    ("retry_after", "expected"),
    [("2", [2.0]), ("-1", [0.1]), ("Wed, 21 Oct 2015 07:28:00 GMT", [0.1])],
)
def test_retry_after_header_overrides_backoff(
    config: Config, sleeps: list[float], retry_after: str, expected: list[float]
) -> None:
    statuses = [429, 201]

    def handler(request: httpx.Request) -> httpx.Response:
        if statuses.pop(0) == 429:
            return httpx.Response(429, headers={"Retry-After": retry_after}, text="slow down")
        return _created(request)

    make_client(config, handler, sleeps).enqueue("settlement")
    assert sleeps == expected


def test_backoff_is_capped_and_jittered() -> None:
    config = Config(backoff_initial_seconds=1.0, backoff_max_seconds=4.0)
    client = JobClient(config, jitter=lambda low, high: (low, high))
    assert [client.backoff_delay(n) for n in (1, 2, 3, 4)] == [
        (0.0, 1.0),
        (0.0, 2.0),
        (0.0, 4.0),
        (0.0, 4.0),
    ]


def test_non_json_response_body_is_preserved(config: Config) -> None:
    result = make_client(config, lambda request: httpx.Response(201, text="created")).enqueue(
        "settlement"
    )
    assert result.body == {"raw": "created"}
    assert result.job_id is None


def test_json_array_response_body_is_wrapped(config: Config) -> None:
    result = make_client(config, lambda request: httpx.Response(201, json=[1, 2])).enqueue(
        "settlement"
    )
    assert result.body == {"raw": [1, 2]}


def test_owns_its_http_client_when_none_is_injected(
    config: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The default path builds (and closes) its own client with the configured timeout."""
    timeouts: list[float] = []
    real_client = httpx.Client

    def fake_client(*, timeout: float) -> httpx.Client:
        timeouts.append(timeout)
        return real_client(transport=httpx.MockTransport(_created))

    monkeypatch.setattr(httpx, "Client", fake_client)
    result = JobClient(config).enqueue("settlement")

    assert (result.status_code, timeouts) == (201, [config.timeout_seconds])
