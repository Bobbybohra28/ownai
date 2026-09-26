"""Exception → HTTP mapping. Clients always get {"error": {code, message, hint?, request_id}};
technical details go to the logs only."""

from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException
from structlog.contextvars import get_contextvars

from app.core.exceptions import AppError, ErrorCode
from app.core.logging import get_logger

log = get_logger("api.errors")


def _body(code: str, message: str, *, hint: str | None = None, data: dict | None = None) -> dict:
    error: dict = {"code": code, "message": message, "request_id": get_contextvars().get("request_id")}
    if hint:
        error["hint"] = hint
    if data:
        error["data"] = data
    return {"error": error}


def install_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(AppError)
    async def app_error(request: Request, exc: AppError) -> JSONResponse:
        level = log.warning if exc.status_code >= 500 else log.info
        level("request.app_error", code=str(exc.code), error=exc.message, detail=exc.detail, path=request.url.path)
        return JSONResponse(status_code=exc.status_code, content=_body(str(exc.code), exc.message, hint=exc.hint,
                                                                       data=exc.data or None))

    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
        problems = []
        for err in exc.errors()[:10]:
            location = ".".join(str(p) for p in err.get("loc", []) if p not in ("body", "query", "path"))
            problems.append(f"{location or 'request'}: {err.get('msg')}")
        return JSONResponse(status_code=422, content=_body(str(ErrorCode.VALIDATION_ERROR),
                                                           "Some fields are invalid: " + "; ".join(problems)))

    @app.exception_handler(StarletteHTTPException)
    async def http_error(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        code = {404: ErrorCode.NOT_FOUND, 401: ErrorCode.AUTHENTICATION_REQUIRED, 403: ErrorCode.PERMISSION_DENIED,
                405: ErrorCode.VALIDATION_ERROR}.get(exc.status_code, ErrorCode.INTERNAL_ERROR)
        message = "The requested endpoint does not exist." if exc.status_code == 404 else str(exc.detail)
        return JSONResponse(status_code=exc.status_code, content=_body(str(code), message))

    @app.exception_handler(Exception)
    async def unhandled(request: Request, exc: Exception) -> JSONResponse:
        log.exception("request.unhandled_error", path=request.url.path)
        return JSONResponse(status_code=500, content=_body(
            str(ErrorCode.INTERNAL_ERROR),
            "Something went wrong on the server. The error was logged; please retry or contact your administrator.",
        ))
