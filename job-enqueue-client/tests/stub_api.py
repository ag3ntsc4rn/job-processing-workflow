"""Stub jobs API for smoke-testing the real CLI over real HTTP.

Mimics the contract the client targets — ``POST /v1/jobs`` returning ``201`` with
a ``job_id``, ``409`` for an already-active job type — and, like the real API
today, does not verify the bearer token. It only records the scopes it saw so a
smoke test can assert they were sent. ``--fail-first N`` makes the first N
requests answer ``503`` to exercise retry + backoff.

    python tests/stub_api.py --port 8099 [--fail-first 2]
"""

from __future__ import annotations

import argparse
import base64
import json
from http.server import BaseHTTPRequestHandler, HTTPServer


def _scopes_from_bearer(header: str) -> list[str]:
    token = header.removeprefix("Bearer ").strip()
    parts = token.split(".")
    if len(parts) != 3:
        return []
    padded = parts[1] + "=" * (-len(parts[1]) % 4)
    claims = json.loads(base64.urlsafe_b64decode(padded))
    return str(claims.get("scope", "")).split()


class StubJobsAPI(BaseHTTPRequestHandler):
    fail_first = 0
    requests_seen = 0
    active_job_types: set[str] = set()
    seen: list[dict] = []
    next_job_id = 1

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002 - stdlib signature
        pass

    def _reply(self, status: int, body: dict) -> None:
        raw = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self) -> None:  # noqa: N802 - stdlib signature
        if self.path == "/healthz":
            self._reply(200, {"status": "ok"})
        elif self.path == "/_seen":
            self._reply(200, {"requests": type(self).seen})
        else:
            self._reply(404, {"title": "not found"})

    def do_POST(self) -> None:  # noqa: N802 - stdlib signature
        cls = type(self)
        if self.path != "/v1/jobs":
            self._reply(404, {"title": "not found"})
            return

        length = int(self.headers.get("Content-Length", "0"))
        body = json.loads(self.rfile.read(length) or b"{}")
        cls.requests_seen += 1
        cls.seen.append(
            {
                "job_type": body.get("job_type"),
                "payload": body.get("payload"),
                "scopes": _scopes_from_bearer(self.headers.get("Authorization", "")),
            }
        )

        if cls.requests_seen <= cls.fail_first:
            self._reply(503, {"title": "service unavailable"})
            return

        job_type = body.get("job_type")
        if job_type in cls.active_job_types:
            self._reply(409, {"title": "an active job of this type already exists"})
            return
        cls.active_job_types.add(job_type)
        job_id = cls.next_job_id
        cls.next_job_id += 1
        self._reply(201, {"job_id": job_id, "job_type": job_type, "status": "queued"})


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8099)
    parser.add_argument("--fail-first", type=int, default=0, help="answer 503 N times first")
    args = parser.parse_args()
    StubJobsAPI.fail_first = args.fail_first
    HTTPServer(("127.0.0.1", args.port), StubJobsAPI).serve_forever()


if __name__ == "__main__":
    main()
