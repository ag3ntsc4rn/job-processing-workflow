"""Cross-cutting HTTP middleware: correlation ids + security headers.

Edge concerns (rate limiting, CORS, TLS) are owned by the API gateway (Apigee),
so v2 keeps only what stays useful behind a gateway: a request-id echoed back for
correlation and defensive security headers.
"""

from __future__ import annotations

import uuid

from starlette.types import ASGIApp, Message, Receive, Scope, Send

_SECURITY_HEADERS = {
    b"x-content-type-options": b"nosniff",
    b"x-frame-options": b"DENY",
    b"referrer-policy": b"no-referrer",
    b"content-security-policy": b"default-src 'none'; frame-ancestors 'none'",
    b"strict-transport-security": b"max-age=63072000; includeSubDomains",
}

REQUEST_ID_HEADER = "x-request-id"


class SecurityMiddleware:
    """Adds a request id (echoed back) and hardening headers to each response."""

    def __init__(self, app: ASGIApp) -> None:
        self._app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return

        headers = dict(scope.get("headers") or [])
        request_id = headers.get(REQUEST_ID_HEADER.encode(), uuid.uuid4().hex.encode())

        async def send_wrapper(message: Message) -> None:
            if message["type"] == "http.response.start":
                raw = message.setdefault("headers", [])
                existing = {k.lower() for k, _ in raw}
                for key, value in _SECURITY_HEADERS.items():
                    if key not in existing:
                        raw.append((key, value))
                raw.append((REQUEST_ID_HEADER.encode(), request_id))
            await send(message)

        await self._app(scope, receive, send_wrapper)
