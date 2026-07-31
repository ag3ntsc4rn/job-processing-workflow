"""HTTP client: POST {job_type, payload} to the jobs API, with retry + backoff."""

from __future__ import annotations

import random
import time
from collections.abc import Callable
from dataclasses import dataclass

import httpx

from jobclient.config import Config
from jobclient.tokens import mint_jwt


class EnqueueError(RuntimeError):
    """Raised when the job could not be enqueued after every attempt."""

    def __init__(self, message: str, *, attempts: int, status_code: int | None = None) -> None:
        super().__init__(message)
        self.attempts = attempts
        self.status_code = status_code


@dataclass(frozen=True)
class EnqueueResult:
    status_code: int
    attempts: int
    body: dict

    @property
    def job_id(self) -> int | None:
        job_id = self.body.get("job_id")
        return job_id if isinstance(job_id, int) else None

    @property
    def duplicate(self) -> bool:
        """True when the API rejected the enqueue because one is already active."""
        return self.status_code == 409


def _retry_after_seconds(response: httpx.Response) -> float | None:
    raw = response.headers.get("Retry-After", "").strip()
    if not raw:
        return None
    try:
        seconds = float(raw)
    except ValueError:
        return None
    return seconds if seconds >= 0 else None


def _body_of(response: httpx.Response) -> dict:
    try:
        body = response.json()
    except ValueError:
        return {"raw": response.text}
    return body if isinstance(body, dict) else {"raw": body}


class JobClient:
    """Thin, retrying wrapper over ``POST {api_url}{jobs_path}``.

    Retries cover the failures a gateway-fronted API actually produces —
    connection/timeout errors and the transient statuses in
    ``Config.retry_status_codes`` — with exponential backoff, full jitter, and
    ``Retry-After`` taking precedence when the server states a delay. Any other
    status (4xx, including ``409`` duplicate) is returned as-is: retrying it
    would never succeed.
    """

    def __init__(
        self,
        config: Config,
        *,
        client: httpx.Client | None = None,
        sleep: Callable[[float], None] = time.sleep,
        jitter: Callable[[float], float] = random.uniform,
    ) -> None:
        self._config = config
        self._client = client
        self._sleep = sleep
        self._jitter = jitter

    def backoff_delay(self, attempt: int) -> float:
        """Full-jitter exponential backoff for the wait after ``attempt`` (1-based)."""
        ceiling = min(
            self._config.backoff_max_seconds,
            self._config.backoff_initial_seconds * 2 ** (attempt - 1),
        )
        return self._jitter(0.0, ceiling)

    def enqueue(self, job_type: str, payload: dict | None = None) -> EnqueueResult:
        if not job_type:
            raise ValueError("job_type must be a non-empty string")
        body: dict = {"job_type": job_type}
        if payload:
            body["payload"] = payload
        headers = {
            "Authorization": f"Bearer {mint_jwt(self._config)}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

        if self._client is not None:
            return self._post_with_retries(self._client, body, headers)
        with httpx.Client(timeout=self._config.timeout_seconds) as client:
            return self._post_with_retries(client, body, headers)

    def _post_with_retries(self, client: httpx.Client, body: dict, headers: dict) -> EnqueueResult:
        url = self._config.jobs_url
        attempts = self._config.max_attempts
        last_error: str = "no attempt was made"
        last_status: int | None = None
        retry_after: float | None = None

        for attempt in range(1, attempts + 1):
            try:
                response = client.post(url, json=body, headers=headers)
            except httpx.HTTPError as exc:
                last_error = f"{type(exc).__name__}: {exc}"
                last_status = None
                retry_after = None
            else:
                if response.status_code not in self._config.retry_status_codes:
                    return EnqueueResult(
                        status_code=response.status_code,
                        attempts=attempt,
                        body=_body_of(response),
                    )
                last_error = f"HTTP {response.status_code}: {response.text[:200]}"
                last_status = response.status_code
                retry_after = _retry_after_seconds(response)

            if attempt == attempts:
                break
            self._sleep(retry_after if retry_after is not None else self.backoff_delay(attempt))

        raise EnqueueError(
            f"enqueue failed after {attempts} attempt(s): {last_error}",
            attempts=attempts,
            status_code=last_status,
        )
