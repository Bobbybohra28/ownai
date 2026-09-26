"""Model router: deterministic, explainable model selection with health-aware fallback.

Selection = hard filters (enabled, health-verified, capabilities, context window)
followed by a transparent score (role match, configured priority, task complexity,
observed latency/success, live load). The largest model is never assumed best.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Literal

from pydantic import BaseModel, Field

from app.core.exceptions import ErrorCode, ModelError
from app.core.logging import get_logger
from app.models.health import ModelHealthService
from app.models.providers.base import (
    CHAT_ROLES,
    ChatChunk,
    ChatRequest,
    ChatResponse,
    EmbedResponse,
    HealthStatus,
    ModelConfig,
    ModelProvider,
    ModelRole,
    RerankResult,
)
from app.models.registry import ModelRegistry
from app.models.tokens import estimate_messages

log = get_logger(__name__)

Complexity = Literal["trivial", "simple", "moderate", "complex"]


class ModelRequirements(BaseModel):
    role: ModelRole
    complexity: Complexity = "simple"
    needs_tools: bool = False
    needs_vision: bool = False
    needs_json: bool = False
    min_context: int = 0
    latency_sensitive: bool = False
    exclude_models: set[str] = Field(default_factory=set)  # hard exclusion (e.g. models that just failed)
    avoid_models: set[str] = Field(default_factory=set)  # soft preference (e.g. critic independence)
    agent_id: str | None = None


class CandidateScore(BaseModel):
    model_id: str
    score: float
    reasons: list[str]
    eligible: bool
    rejected_reason: str | None = None


class RoutingDecision(BaseModel):
    model_id: str
    model_name: str
    role: ModelRole
    reason: str
    candidates: list[CandidateScore] = Field(default_factory=list)
    fallback_from: list[str] = Field(default_factory=list)
    degraded: bool = False
    notices: list[str] = Field(default_factory=list)


class RoutedChatResult(BaseModel):
    response: ChatResponse
    decision: RoutingDecision


@dataclass
class UsageEvent:
    model_id: str
    role: str
    operation: str
    success: bool
    latency_ms: int
    prompt_tokens: int = 0
    completion_tokens: int = 0
    error_code: str | None = None
    fallback_from: str | None = None
    routing_reason: str = ""
    context: dict[str, Any] = field(default_factory=dict)


UsageSink = Callable[[UsageEvent], Awaitable[None]]


@dataclass
class _Stats:
    calls: int = 0
    failures: int = 0
    latency_ms_ewma: float | None = None

    def record(self, success: bool, latency_ms: int) -> None:
        self.calls += 1
        if not success:
            self.failures += 1
        elif latency_ms:
            self.latency_ms_ewma = latency_ms if self.latency_ms_ewma is None else 0.8 * self.latency_ms_ewma + 0.2 * latency_ms

    @property
    def success_rate(self) -> float | None:
        return None if self.calls < 3 else (self.calls - self.failures) / self.calls


class NoModelAvailable(ModelError):
    default_code = ErrorCode.MODEL_UNAVAILABLE


class ModelRouter:
    def __init__(self, registry: ModelRegistry, providers: dict[str, ModelProvider], health: ModelHealthService,
                 usage_sink: UsageSink | None = None) -> None:
        self.registry = registry
        self.providers = providers
        self.health = health
        self.usage_sink = usage_sink
        self._stats: dict[str, _Stats] = {}
        self.quality_scores: dict[tuple[str, str], float] = {}  # (model_id, role) -> 0..1 from evaluations

    # ---- selection --------------------------------------------------------------------------
    def _capability_problem(self, m: ModelConfig, req: ModelRequirements) -> str | None:
        caps = m.capabilities
        if req.role == ModelRole.EMBEDDING:
            return None if caps.supports_embeddings else "no embedding support"
        if req.role == ModelRole.RERANKER:
            return None if caps.supports_rerank else "no rerank support"
        if not caps.supports_chat:
            return "not a chat model"
        if req.needs_vision and not caps.supports_vision:
            return "no vision support"
        if req.min_context and m.context_length < req.min_context:
            return f"context {m.context_length} < required {req.min_context}"
        return None

    def _score(self, m: ModelConfig, req: ModelRequirements) -> tuple[float, list[str]]:
        reasons: list[str] = []
        score = float(m.priority)
        reasons.append(f"priority {m.priority}")
        if req.role in m.roles:
            score += 100
            reasons.append(f"has role '{req.role}'")
        else:
            reasons.append(f"lacks role '{req.role}' (fallback only)")
        if req.role in CHAT_ROLES:
            if req.complexity == "complex" and ModelRole.REASONING in m.roles and self.registry.policy.prefer_reasoning_for_complex:
                score += 25
                reasons.append("reasoning model preferred for complex task")
            if req.complexity in ("trivial", "simple") and ModelRole.FAST in m.roles:
                score += 15
                reasons.append("fast model preferred for simple task")
            if req.needs_tools and m.capabilities.supports_tools:
                score += 10
                reasons.append("native tool calling")
        quality = self.quality_scores.get((m.id, str(req.role)))
        if quality is not None:
            score += 30 * quality
            reasons.append(f"benchmark quality {quality:.2f}")
        stats = self._stats.get(m.id)
        if stats and stats.success_rate is not None:
            score += 20 * stats.success_rate - 20
            reasons.append(f"success rate {stats.success_rate:.0%}")
        if req.latency_sensitive and stats and stats.latency_ms_ewma:
            penalty = min(stats.latency_ms_ewma / 200, 25)
            score -= penalty
            reasons.append(f"latency ~{stats.latency_ms_ewma:.0f}ms")
        return score, reasons

    async def is_healthy(self, model_id: str) -> tuple[bool, str | None]:
        try:
            report = await self.health.ensure_checked(model_id)
        except ModelError as exc:
            return False, exc.message
        if report.status in (HealthStatus.ONLINE, HealthStatus.DEGRADED):
            return True, None
        return False, f"health {report.status}: {report.error or report.error_code or 'unknown'}"

    async def rank(self, req: ModelRequirements) -> list[CandidateScore]:
        pool = self.registry.enabled()
        if req.role in CHAT_ROLES:
            pool = [m for m in pool if m.capabilities.supports_chat]
            if not self.registry.policy.allow_cross_role_fallback:
                pool = [m for m in pool if req.role in m.roles]
        else:
            pool = [m for m in pool if req.role in m.roles]
        preferred = self.registry.policy.role_preferences.get(req.role, [])
        candidates: list[CandidateScore] = []
        health_results = await asyncio.gather(*(self.is_healthy(m.id) for m in pool))
        for m, (healthy, health_reason) in zip(pool, health_results, strict=True):
            problem = self._capability_problem(m, req)
            if m.id in req.exclude_models:
                problem = problem or "excluded (failed in this request)"
            if problem is None and not healthy:
                problem = health_reason or "unhealthy"
            score, reasons = self._score(m, req)
            if m.id in req.avoid_models:
                # independence is a preference that must never outweigh capability (role bonus is 100)
                score -= 40
                reasons.append("same model as the producer (independence preferred)")
            if m.id in preferred:
                bonus = 50 - 5 * preferred.index(m.id)
                score += bonus
                reasons.append("configured preference")
            candidates.append(CandidateScore(model_id=m.id, score=round(score, 2), reasons=reasons,
                                             eligible=problem is None, rejected_reason=problem))
        candidates.sort(key=lambda c: (not c.eligible, -c.score))
        return candidates

    async def select(self, req: ModelRequirements) -> RoutingDecision:
        ranked = await self.rank(req)
        eligible = [c for c in ranked if c.eligible]
        if not eligible:
            details = "; ".join(f"{c.model_id}: {c.rejected_reason}" for c in ranked) or "no models configured"
            raise NoModelAvailable(
                f"No healthy model is available for the '{req.role}' role.",
                code=ErrorCode.MODEL_UNAVAILABLE,
                detail=details,
                hint="Open the Models page to see which endpoints are offline, or configure a model for this role.",
                data={"candidates": [c.model_dump() for c in ranked]},
            )
        decision = self._decision(eligible[0], req, ranked)
        if decision.model_id in req.avoid_models:
            decision.notices.append("No independent model was available; the same model was used for verification.")
        return decision

    def _decision(self, chosen: CandidateScore, req: ModelRequirements, ranked: list[CandidateScore]) -> RoutingDecision:
        config = self.registry.get(chosen.model_id)
        assert config is not None
        degraded = req.role not in config.roles and bool(self.registry.by_role(req.role))
        notices: list[str] = []
        if req.role not in config.roles:
            if self.registry.by_role(req.role):
                notices.append(f"The dedicated {req.role} model is unavailable; '{config.id}' was used instead.")
            elif req.role != ModelRole.FAST:
                notices.append(f"No dedicated {req.role} model is configured; using '{config.id}'.")
        return RoutingDecision(
            model_id=config.id, model_name=config.model, role=req.role,
            reason="; ".join(chosen.reasons), candidates=ranked, degraded=degraded, notices=notices,
        )

    # ---- execution --------------------------------------------------------------------------
    async def _record(self, event: UsageEvent) -> None:
        self._stats.setdefault(event.model_id, _Stats()).record(event.success, event.latency_ms)
        if self.usage_sink:
            try:
                await self.usage_sink(event)
            except Exception as exc:
                log.warning("router.usage_sink_failed", error=str(exc))

    def _with_context_requirement(self, req: ModelRequirements, request: ChatRequest) -> ModelRequirements:
        needed = estimate_messages(request.messages) + (request.max_tokens or 512)
        return req.model_copy(update={"min_context": max(req.min_context, needed)})

    async def chat(self, request: ChatRequest, req: ModelRequirements, *,
                   context: dict[str, Any] | None = None, max_attempts: int = 3) -> RoutedChatResult:
        req = self._with_context_requirement(req, request)
        failed: list[str] = []
        last_error: ModelError | None = None
        for _ in range(max_attempts):
            try:
                decision = await self.select(req.model_copy(update={"exclude_models": req.exclude_models | set(failed)}))
            except NoModelAvailable as exc:
                if last_error is not None:
                    raise ModelError(
                        f"All candidate models failed. Last error: {last_error.message}",
                        code=last_error.code, detail=f"tried={failed}; {exc.detail}", hint=last_error.hint,
                        model_id=last_error.model_id,
                    ) from last_error
                raise
            provider = self.providers[decision.model_id]
            started = time.perf_counter()
            try:
                response = await provider.chat(request)
            except ModelError as exc:
                latency = int((time.perf_counter() - started) * 1000)
                await self._record(UsageEvent(decision.model_id, str(req.role), "chat", False, latency,
                                              error_code=str(exc.code), routing_reason=decision.reason,
                                              context=context or {}))
                await self.health.report_failure(decision.model_id, exc)
                log.warning("router.model_failed", model_id=decision.model_id, code=exc.code, error=exc.message,
                            detail=exc.detail, agent_id=req.agent_id)
                last_error = exc
                if not exc.retryable:
                    raise
                failed.append(decision.model_id)
                continue
            decision.fallback_from = failed
            if failed:
                decision.notices.append(
                    f"Model(s) {', '.join(failed)} failed ({last_error.code if last_error else 'error'}); "
                    f"'{decision.model_id}' answered instead."
                )
            await self._record(UsageEvent(
                decision.model_id, str(req.role), "chat", True, response.latency_ms,
                response.usage.prompt_tokens, response.usage.completion_tokens,
                fallback_from=failed[-1] if failed else None, routing_reason=decision.reason, context=context or {},
            ))
            return RoutedChatResult(response=response, decision=decision)
        assert last_error is not None
        raise last_error

    async def stream_chat(self, request: ChatRequest, req: ModelRequirements, *,
                          context: dict[str, Any] | None = None) -> tuple[RoutingDecision, AsyncIterator[ChatChunk]]:
        """Select a model and return its stream. Fallback happens only before the first chunk."""
        req = self._with_context_requirement(req, request)
        failed: list[str] = []
        last_error: ModelError | None = None
        for _ in range(3):
            try:
                decision = await self.select(req.model_copy(update={"exclude_models": req.exclude_models | set(failed)}))
            except NoModelAvailable:
                if last_error:
                    raise last_error from None
                raise
            provider = self.providers[decision.model_id]
            stream = provider.stream_chat(request)
            started = time.perf_counter()
            try:
                first = await anext(stream)
            except StopAsyncIteration:
                last_error = ModelError("The model stream ended without data.", code=ErrorCode.MODEL_STREAM_ERROR,
                                        model_id=decision.model_id)
                failed.append(decision.model_id)
                continue
            except ModelError as exc:
                await self._record(UsageEvent(decision.model_id, str(req.role), "stream", False,
                                              int((time.perf_counter() - started) * 1000), error_code=str(exc.code),
                                              context=context or {}))
                await self.health.report_failure(decision.model_id, exc)
                last_error = exc
                if not exc.retryable:
                    raise
                failed.append(decision.model_id)
                continue
            decision.fallback_from = failed
            if failed:
                decision.notices.append(f"Model(s) {', '.join(failed)} failed; '{decision.model_id}' answered instead.")
            return decision, self._wrap_stream(first, stream, decision, req, started, context or {})
        assert last_error is not None
        raise last_error

    async def _wrap_stream(self, first: ChatChunk, stream: AsyncIterator[ChatChunk], decision: RoutingDecision,
                           req: ModelRequirements, started: float, context: dict[str, Any]) -> AsyncIterator[ChatChunk]:
        prompt_tokens = completion_tokens = 0
        success = False
        try:
            yield first
            async for chunk in stream:
                if chunk.usage:
                    prompt_tokens, completion_tokens = chunk.usage.prompt_tokens, chunk.usage.completion_tokens
                yield chunk
            success = True
        finally:
            await self._record(UsageEvent(decision.model_id, str(req.role), "stream", success,
                                          int((time.perf_counter() - started) * 1000), prompt_tokens, completion_tokens,
                                          routing_reason=decision.reason, context=context))

    async def embed(self, texts: list[str], *, context: dict[str, Any] | None = None) -> EmbedResponse:
        decision = await self.select(ModelRequirements(role=ModelRole.EMBEDDING))
        provider = self.providers[decision.model_id]
        started = time.perf_counter()
        try:
            result = await provider.embed(texts)
        except ModelError as exc:
            await self._record(UsageEvent(decision.model_id, "embedding", "embed", False,
                                          int((time.perf_counter() - started) * 1000), error_code=str(exc.code),
                                          context=context or {}))
            await self.health.report_failure(decision.model_id, exc)
            raise
        await self._record(UsageEvent(decision.model_id, "embedding", "embed", True, result.latency_ms,
                                      result.usage.prompt_tokens, context=context or {}))
        return result

    def embedding_model(self) -> ModelConfig | None:
        models = self.registry.by_role(ModelRole.EMBEDDING)
        return max(models, key=lambda m: m.priority) if models else None

    async def rerank(self, query: str, documents: list[str], top_n: int | None = None) -> list[RerankResult]:
        decision = await self.select(ModelRequirements(role=ModelRole.RERANKER))
        provider = self.providers[decision.model_id]
        started = time.perf_counter()
        try:
            results = await provider.rerank(query, documents, top_n)
        except ModelError as exc:
            await self.health.report_failure(decision.model_id, exc)
            raise
        await self._record(UsageEvent(decision.model_id, "reranker", "rerank", True,
                                      int((time.perf_counter() - started) * 1000)))
        return results
