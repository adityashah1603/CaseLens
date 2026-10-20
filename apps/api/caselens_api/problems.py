"""RFC 9457 problem details (`application/problem+json`) for every error response."""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

PROBLEM_MEDIA_TYPE = "application/problem+json"
PROBLEM_BASE = "https://caselens.dev/problems/"

_TITLES = {
    400: "Bad request",
    401: "Unauthorized",
    403: "Forbidden",
    404: "Not found",
    405: "Method not allowed",
    409: "Conflict",
    413: "Payload too large",
    415: "Unsupported media type",
    422: "Validation error",
    429: "Too many requests",
    500: "Internal server error",
    503: "Service unavailable",
}


class ProblemError(Exception):
    """Raise anywhere in a request to return a problem+json response."""

    def __init__(
        self,
        status: int,
        detail: str | None = None,
        *,
        title: str | None = None,
        type_: str | None = None,
        headers: dict[str, str] | None = None,
        errors: list[dict[str, Any]] | None = None,
    ) -> None:
        super().__init__(detail or title or str(status))
        self.status = status
        self.detail = detail
        self.title = title or _TITLES.get(status, "Error")
        self.type = PROBLEM_BASE + type_ if type_ else "about:blank"
        self.headers = headers
        self.errors = errors


def unauthorized(detail: str = "Missing, expired or invalid bearer token.") -> ProblemError:
    return ProblemError(401, detail, headers={"WWW-Authenticate": "Bearer"})


def not_found(detail: str = "The resource does not exist.") -> ProblemError:
    return ProblemError(404, detail)


def forbidden(detail: str = "Your role on this case does not allow this action.") -> ProblemError:
    return ProblemError(403, detail)


def conflict(detail: str, type_: str = "conflict") -> ProblemError:
    return ProblemError(409, detail, type_=type_)


def validation(detail: str, loc: list[str | int] | None = None) -> ProblemError:
    errors = [{"loc": loc, "msg": detail}] if loc is not None else None
    return ProblemError(422, detail, errors=errors)


def problem_response(request: Request, exc: ProblemError) -> JSONResponse:
    body: dict[str, Any] = {
        "type": exc.type,
        "title": exc.title,
        "status": exc.status,
        "instance": request.url.path,
    }
    if exc.detail:
        body["detail"] = exc.detail
    if exc.errors is not None:
        body["errors"] = exc.errors
    return JSONResponse(
        body, status_code=exc.status, headers=exc.headers, media_type=PROBLEM_MEDIA_TYPE
    )


def _loc(raw: Any) -> list[str | int]:
    return [p if isinstance(p, int) else str(p) for p in raw]


def install_problem_handlers(app: FastAPI) -> None:
    async def on_problem(request: Request, exc: Exception) -> JSONResponse:
        if not isinstance(exc, ProblemError):
            raise exc
        return problem_response(request, exc)

    async def on_validation(request: Request, exc: Exception) -> JSONResponse:
        if not isinstance(exc, RequestValidationError):
            raise exc
        errors = [
            {"loc": _loc(e.get("loc", ())), "msg": str(e.get("msg", ""))} for e in exc.errors()
        ]
        return problem_response(
            request, ProblemError(422, "The request failed validation.", errors=errors)
        )

    async def on_http(request: Request, exc: Exception) -> JSONResponse:
        if not isinstance(exc, StarletteHTTPException):
            raise exc
        headers = dict(exc.headers) if exc.headers else None
        detail = exc.detail if isinstance(exc.detail, str) else None
        return problem_response(request, ProblemError(exc.status_code, detail, headers=headers))

    async def on_error(request: Request, exc: Exception) -> JSONResponse:
        return problem_response(request, ProblemError(500, "Unexpected server error."))

    app.add_exception_handler(ProblemError, on_problem)
    app.add_exception_handler(RequestValidationError, on_validation)
    app.add_exception_handler(StarletteHTTPException, on_http)
    app.add_exception_handler(Exception, on_error)
