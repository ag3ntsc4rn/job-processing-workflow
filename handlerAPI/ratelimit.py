"""In-app rate limiting.

Built directly on the ``limits`` library (the same engine slowapi wraps) so it
doesn't depend on any web-framework router internals. It runs as pure ASGI
middleware — before route dependencies — keyed by the caller's bearer credential
(a hash of the token, so distinct clients get distinct buckets and one noisy
client can't starve others), falling back to client IP for unauthenticated hits.

This is the in-app layer; a shared API gateway / LB limiter is the intended
eventual home, at which point this stays as defence in depth.
"""

from __future__ import annotations

import hashlib

from limits import RateLimitItem, parse
from limits.storage import MemoryStorage
from limits.strategies import MovingWindowRateLimiter
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from handlerAPI.config import Settings
from handlerAPI.errors import PROBLEM_MEDIA_TYPE


def _key(request: Request) -> str:
    auth = request.headers.get("Authorization", "")
    scheme, _, token = auth.partition(" ")
    if scheme.lower() == "bearer" and token.strip():
        digest = hashlib.sha256(token.strip().encode()).hexdigest()[:32]
        return f"tok:{digest}"
    client = request.client
    return client.host if client else "anonymous"


class RateLimitMiddleware:
    """Sliding-window limiter; returns an RFC 7807 429 when a caller exceeds it."""

    def __init__(self, app: ASGIApp, *, settings: Settings) -> None:
        self._app = app
        self._enabled = settings.rate_limit_enabled
        self._item: RateLimitItem = parse(settings.rate_limit)
        self._limiter = MovingWindowRateLimiter(MemoryStorage())

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or not self._enabled:
            await self._app(scope, receive, send)
            return

        request = Request(scope, receive=receive)
        if not self._limiter.hit(self._item, _key(request)):
            response = JSONResponse(
                status_code=429,
                media_type=PROBLEM_MEDIA_TYPE,
                content={
                    "type": "about:blank",
                    "title": "Too Many Requests",
                    "status": 429,
                    "detail": f"rate limit exceeded: {self._item}",
                    "instance": request.url.path,
                },
            )
            await response(scope, receive, send)
            return

        await self._app(scope, receive, send)
