"""FastAPI application factory. Run with: uvicorn app.main:create_app --factory"""

from __future__ import annotations

import time
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware

from app.api.errors import install_error_handlers
from app.api.v1 import (
    admin,
    agents,
    approvals,
    auth,
    billing,
    conversations,
    evaluation,
    files,
    git,
    memory,
    models,
    organizations,
    projects,
    rag,
    runs,
    sql,
    tools,
    users,
)
from app.core.config import Settings, get_settings
from app.core.dependencies import Container, build_container
from app.core.logging import bind_contextvars, clear_contextvars, configure_logging, get_logger

log = get_logger("api")


def create_app(settings: Settings | None = None, container: Container | None = None) -> FastAPI:
    settings = settings or get_settings()
    configure_logging(settings.log_level, settings.log_json)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        owned = container is None
        app.state.container = container or await build_container(settings, role="api")
        if settings.execution_mode == "inline":
            app.state.container.health.start_background(settings.model_health_interval_s)
        try:
            yield
        finally:
            if owned:
                await app.state.container.aclose()

    app = FastAPI(title="OwnAI API", version="0.1.0", lifespan=lifespan,
                  docs_url=f"{settings.api_prefix}/docs", openapi_url=f"{settings.api_prefix}/openapi.json",
                  description="Private multi-model, multi-agent AI developer platform.")
    app.add_middleware(CORSMiddleware, allow_origins=settings.cors_origins, allow_credentials=True,
                       allow_methods=["*"], allow_headers=["*"], expose_headers=["X-Request-ID"])

    @app.middleware("http")
    async def request_context(request: Request, call_next: Any) -> Response:
        request_id = request.headers.get("x-request-id") or uuid.uuid4().hex
        clear_contextvars()
        bind_contextvars(request_id=request_id[:64], client_ip=request.client.host if request.client else None)
        started = time.perf_counter()
        response: Response = await call_next(request)
        response.headers["X-Request-ID"] = request_id[:64]
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        if not request.url.path.endswith("/events"):
            log.info("request", method=request.method, path=request.url.path, status=response.status_code,
                     duration_ms=int((time.perf_counter() - started) * 1000))
        return response

    install_error_handlers(app)
    for module in (auth, users, organizations, projects, files, conversations, runs, approvals, models, agents, tools,
                   rag, memory, git, sql, evaluation, admin, billing):
        app.include_router(module.router, prefix=settings.api_prefix)

    @app.get("/health", tags=["system"])
    async def health(request: Request) -> dict[str, Any]:
        """Liveness + dependency status (models are reported from their last real health check)."""
        checks = await admin.service_health(request.app.state.container)
        core_ok = checks["database"].get("ok") and checks["redis"].get("ok")
        return {"status": "ok" if core_ok else "degraded", "checks": checks}

    return app

