"""RFC 7807 ``application/problem+json`` errors and exception handlers.

Every error the API returns is a Problem document so clients get a stable,
machine-readable shape and we never leak stack traces or SQL.
"""

from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

PROBLEM_MEDIA_TYPE = "application/problem+json"


class ProblemException(Exception):
    """Raised anywhere in the request path to return a Problem response."""

    def __init__(
        self,
        status_code: int,
        title: str,
        detail: str | None = None,
        *,
        headers: dict[str, str] | None = None,
    ) -> None:
        super().__init__(detail or title)
        self.status_code = status_code
        self.title = title
        self.detail = detail
        self.headers = headers or {}


def _problem(
    status_code: int,
    title: str,
    detail: str | None,
    request: Request,
    headers: dict[str, str] | None = None,
) -> JSONResponse:
    body: dict[str, object] = {
        "type": "about:blank",
        "title": title,
        "status": status_code,
        "instance": str(request.url.path),
    }
    if detail:
        body["detail"] = detail
    return JSONResponse(
        status_code=status_code,
        content=body,
        media_type=PROBLEM_MEDIA_TYPE,
        headers=headers,
    )


def register_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(ProblemException)
    async def _handle_problem(request: Request, exc: ProblemException) -> JSONResponse:
        return _problem(exc.status_code, exc.title, exc.detail, request, exc.headers)

    @app.exception_handler(StarletteHTTPException)
    async def _handle_http(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        detail = exc.detail if isinstance(exc.detail, str) else None
        headers = getattr(exc, "headers", None)
        return _problem(exc.status_code, "HTTP error", detail, request, headers)

    @app.exception_handler(RequestValidationError)
    async def _handle_validation(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        return _problem(422, "Request validation failed", str(exc.errors()), request)

    @app.exception_handler(Exception)
    async def _handle_unexpected(request: Request, exc: Exception) -> JSONResponse:
        # Never surface internals; the correlation id in logs ties back to this.
        return _problem(500, "Internal server error", None, request)
