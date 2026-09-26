"""Model health: real connectivity + functional probes, shared health state, passive failure tracking.

A model is only reported ``online`` after *all* of these passed:
1. the endpoint answered its model-listing API (``/v1/models`` or ``/api/tags``),
2. the configured model name is served there,
3. a real request (tiny chat completion / embedding / rerank) returned non-empty output.
"""

from __future__ import annotations

import asyncio
import json
import time
from abc import ABC, abstractmethod
from datetime import UTC, datetime
from typing import Any

from app.core.exceptions import AppError, ErrorCode, ModelError
from app.core.logging import get_logger
from app.models.providers.base import (
    ChatMessage,
    ChatRequest,
    HealthCheckStep,
    HealthReport,
    HealthStatus,
    ModelConfig,
    ModelProvider,
)
from app.models.registry import ModelRegistry

log = get_logger(__name__)

PROBE_PROMPT = "Health check. Reply with exactly the word: OK"
# Errors that mean the model cannot currently serve anything.
_HARD_FAILURES = {
    ErrorCode.MODEL_CONNECTION_ERROR,
    ErrorCode.MODEL_TIMEOUT,
    ErrorCode.MODEL_AUTH_ERROR,
    ErrorCode.MODEL_WRONG_NAME,
    ErrorCode.MODEL_HTTP_ERROR,
}


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


class HealthStore(ABC):
    @abstractmethod
    async def get(self, model_id: str) -> HealthReport | None: ...

    @abstractmethod
    async def put(self, report: HealthReport) -> None: ...

    @abstractmethod
    async def all(self) -> dict[str, HealthReport]: ...


class InMemoryHealthStore(HealthStore):
    def __init__(self) -> None:
        self._data: dict[str, HealthReport] = {}

    async def get(self, model_id: str) -> HealthReport | None:
        return self._data.get(model_id)

    async def put(self, report: HealthReport) -> None:
        self._data[report.model_id] = report

    async def all(self) -> dict[str, HealthReport]:
        return dict(self._data)


class RedisHealthStore(HealthStore):
    """Shares health between API and worker processes."""

    KEY = "ownai:model_health"

    def __init__(self, redis: Any) -> None:
        self.redis = redis

    async def get(self, model_id: str) -> HealthReport | None:
        raw = await self.redis.hget(self.KEY, model_id)
        return HealthReport.model_validate_json(raw) if raw else None

    async def put(self, report: HealthReport) -> None:
        await self.redis.hset(self.KEY, report.model_id, report.model_dump_json())

    async def all(self) -> dict[str, HealthReport]:
        raw = await self.redis.hgetall(self.KEY)
        return {
            (k.decode() if isinstance(k, bytes) else k): HealthReport.model_validate_json(v)
            for k, v in raw.items()
        }


class ModelHealthService:
    def __init__(self, registry: ModelRegistry, providers: dict[str, ModelProvider], store: HealthStore,
                 *, ttl_s: int = 300) -> None:
        self.registry = registry
        self.providers = providers
        self.store = store
        self.ttl_s = ttl_s
        self._locks: dict[str, asyncio.Lock] = {}
        self._task: asyncio.Task[None] | None = None
        self.on_report: list[Any] = []  # callbacks(report) e.g. persist to DB
        self.functional_interval_s = 600
        self._last_success: dict[str, str] = {}

    # ---- probing ----------------------------------------------------------------------------
    async def check(self, model_id: str, *, deep: bool = False, light: bool = False) -> HealthReport:
        """Probe a model. ``light`` skips the generation probe while a recent functional check exists."""
        config = self.registry.get(model_id)
        if config is None:
            raise AppError(f"Unknown model '{model_id}'.", code=ErrorCode.NOT_FOUND)
        lock = self._locks.setdefault(model_id, asyncio.Lock())
        async with lock:
            previous = await self.store.get(model_id)
            if light and not deep and previous is not None and self._functional_recent(previous):
                report = await self._light_probe(config, previous)
            else:
                report = await self._probe(config, deep=deep)
                if report.status in (HealthStatus.ONLINE, HealthStatus.DEGRADED):
                    report.functional_checked_at = report.checked_at
        await self._save(report)
        return report

    def _functional_recent(self, report: HealthReport) -> bool:
        if report.status not in (HealthStatus.ONLINE, HealthStatus.DEGRADED):
            return False
        stamp = self._last_success.get(report.model_id) or report.functional_checked_at
        if not stamp:
            return False
        try:
            age = (datetime.now(UTC) - datetime.fromisoformat(stamp)).total_seconds()
        except ValueError:
            return False
        return age < self.functional_interval_s

    async def _light_probe(self, config: ModelConfig, previous: HealthReport) -> HealthReport:
        """Reachability + model-name check only; keeps the last verified functional result."""
        provider = self.providers[config.id]
        started = time.perf_counter()
        try:
            available = await provider.list_models()
        except ModelError as exc:
            return HealthReport(model_id=config.id, status=HealthStatus.OFFLINE, checked_at=_now_iso(),
                                error_code=str(exc.code), error=exc.message,
                                steps=[HealthCheckStep(name="list_models", ok=False, code=str(exc.code),
                                                       message=exc.message, latency_ms=_ms(started))])
        if not provider.name_matches(available):
            message = f"The endpoint no longer serves '{config.model}'."
            return HealthReport(model_id=config.id, status=HealthStatus.OFFLINE, checked_at=_now_iso(),
                                error_code=str(ErrorCode.MODEL_WRONG_NAME), error=message, available_models=available,
                                steps=[HealthCheckStep(name="model_name", ok=False, message=message)])
        return previous.model_copy(update={"checked_at": _now_iso(), "available_models": available})

    def record_success(self, model_id: str) -> None:
        """A real request succeeded — counts as a functional health check."""
        self._last_success[model_id] = _now_iso()

    async def _probe(self, config: ModelConfig, *, deep: bool) -> HealthReport:
        if not config.enabled:
            return HealthReport(model_id=config.id, status=HealthStatus.DISABLED, checked_at=_now_iso())
        provider = self.providers[config.id]
        steps: list[HealthCheckStep] = []
        available: list[str] | None = None

        def fail(code: str, message: str, status: HealthStatus = HealthStatus.OFFLINE) -> HealthReport:
            return HealthReport(model_id=config.id, status=status, checked_at=_now_iso(), error_code=code,
                                error=message, steps=steps, available_models=available)

        # 1. endpoint reachable + model listing
        started = time.perf_counter()
        try:
            available = await provider.list_models()
            steps.append(HealthCheckStep(name="list_models", ok=True, latency_ms=_ms(started),
                                         message=f"{len(available)} model(s) served"))
        except ModelError as exc:
            steps.append(HealthCheckStep(name="list_models", ok=False, code=str(exc.code), message=exc.message,
                                         latency_ms=_ms(started)))
            return fail(str(exc.code), exc.message)

        # 2. configured model name is served
        if not provider.name_matches(available):
            message = (f"The endpoint does not serve '{config.model}'. Served: "
                       f"{', '.join(available[:10]) or 'none'}.")
            steps.append(HealthCheckStep(name="model_name", ok=False, code=str(ErrorCode.MODEL_WRONG_NAME), message=message))
            return fail(str(ErrorCode.MODEL_WRONG_NAME), message)
        steps.append(HealthCheckStep(name="model_name", ok=True, message=f"'{config.model}' is served"))
        server_ctx = provider.server_context_lengths.get(config.model)
        context_warning = None
        if server_ctx is not None:
            ok = server_ctx >= config.context_length
            if not ok:
                context_warning = (f"Configured context_length {config.context_length} exceeds the server's "
                                   f"max_model_len {server_ctx}; long requests will fail.")
            steps.append(HealthCheckStep(name="context_window", ok=ok, code=None if ok else "CONTEXT_MISMATCH",
                                         message=context_warning or f"server context {server_ctx} tokens"))

        # 3. functional probe with real output validation
        caps = config.capabilities
        probe_started = time.perf_counter()
        try:
            if caps.supports_chat:
                response = await provider.chat(ChatRequest(
                    messages=[ChatMessage(role="user", content=PROBE_PROMPT)],
                    max_tokens=max(config.health_probe_max_tokens, 16),
                    temperature=0.0,
                ))
                steps.append(HealthCheckStep(name="chat_completion", ok=True, latency_ms=_ms(probe_started),
                                             message=f"received {len(response.content)} chars"))
            elif caps.supports_embeddings:
                emb = await provider.embed(["health check"])
                steps.append(HealthCheckStep(name="embedding", ok=True, latency_ms=_ms(probe_started),
                                             message=f"{emb.dimensions}-dimensional vector"))
            elif caps.supports_rerank:
                results = await provider.rerank("health", ["health check", "unrelated"], top_n=2)
                steps.append(HealthCheckStep(name="rerank", ok=True, latency_ms=_ms(probe_started),
                                             message=f"{len(results)} scores"))
        except ModelError as exc:
            steps.append(HealthCheckStep(name="functional_probe", ok=False, code=str(exc.code),
                                         message=exc.message + (f" Hint: {exc.hint}" if exc.hint else ""),
                                         latency_ms=_ms(probe_started)))
            return fail(str(exc.code), exc.message)
        latency = _ms(probe_started)

        # 4. optional streaming probe
        status = HealthStatus.ONLINE
        if deep and caps.supports_chat and caps.supports_streaming:
            stream_started = time.perf_counter()
            try:
                received = ""
                async for chunk in provider.stream_chat(ChatRequest(
                    messages=[ChatMessage(role="user", content=PROBE_PROMPT)],
                    max_tokens=max(config.health_probe_max_tokens, 16), temperature=0.0,
                )):
                    received += chunk.content
                steps.append(HealthCheckStep(name="streaming", ok=True, latency_ms=_ms(stream_started),
                                             message=f"streamed {len(received)} chars"))
            except ModelError as exc:
                steps.append(HealthCheckStep(name="streaming", ok=False, code=str(exc.code), message=exc.message,
                                             latency_ms=_ms(stream_started)))
                status = HealthStatus.DEGRADED
        error_code = None if status == HealthStatus.ONLINE else str(ErrorCode.MODEL_STREAM_ERROR)
        error = None if status == HealthStatus.ONLINE else "Streaming failed; non-streaming works."
        if context_warning:
            status, error_code, error = HealthStatus.DEGRADED, "CONTEXT_MISMATCH", context_warning
        return HealthReport(model_id=config.id, status=status, checked_at=_now_iso(), latency_ms=latency,
                            steps=steps, available_models=available, error_code=error_code, error=error)

    async def _save(self, report: HealthReport) -> None:
        await self.store.put(report)
        for callback in self.on_report:
            try:
                await callback(report)
            except Exception as exc:  # persistence failure must not break health checks
                log.warning("model_health.callback_failed", error=str(exc))
        log.info("model_health.checked", model_id=report.model_id, status=report.status,
                 error_code=report.error_code, latency_ms=report.latency_ms)

    async def check_all(self, *, deep: bool = False, light: bool = False) -> list[HealthReport]:
        configs = self.registry.all()
        return list(await asyncio.gather(*(self.check(c.id, deep=deep, light=light) for c in configs)))

    # ---- state ------------------------------------------------------------------------------
    async def get(self, model_id: str) -> HealthReport | None:
        return await self.store.get(model_id)

    def is_fresh(self, report: HealthReport) -> bool:
        try:
            checked = datetime.fromisoformat(report.checked_at)
        except ValueError:
            return False
        # failed models are re-probed sooner so recovery is detected quickly
        ttl = self.ttl_s if report.status in (HealthStatus.ONLINE, HealthStatus.DEGRADED) else min(self.ttl_s, 15)
        return (datetime.now(UTC) - checked).total_seconds() < ttl

    async def ensure_checked(self, model_id: str) -> HealthReport:
        """Return a fresh report, probing the model if the stored one is missing or stale."""
        report = await self.store.get(model_id)
        if report is None or not self.is_fresh(report):
            report = await self.check(model_id)
        return report

    async def report_failure(self, model_id: str, error: ModelError) -> None:
        """Passive health: a real request failed. Hard failures mark the model offline immediately."""
        if error.code not in _HARD_FAILURES:
            return
        previous = await self.store.get(model_id)
        report = HealthReport(
            model_id=model_id, status=HealthStatus.OFFLINE, checked_at=_now_iso(),
            error_code=str(error.code), error=error.message,
            steps=[HealthCheckStep(name="live_request", ok=False, code=str(error.code), message=error.message)],
            available_models=previous.available_models if previous else None,
        )
        await self._save(report)

    # ---- background loop ----------------------------------------------------------------------
    def start_background(self, interval_s: int) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._loop(interval_s), name="model-health-loop")

    async def stop_background(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    async def _loop(self, interval_s: int) -> None:
        while True:
            try:
                await self.check_all(light=True)
            except Exception as exc:
                log.error("model_health.loop_error", error=str(exc))
            await asyncio.sleep(interval_s)


def _ms(started: float) -> int:
    return int((time.perf_counter() - started) * 1000)


def summarize(report: HealthReport) -> dict[str, Any]:
    return json.loads(report.model_dump_json())
