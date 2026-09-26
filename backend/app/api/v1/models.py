"""Model registry, real health checks, connection tests, routing preview, usage stats."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from fastapi import APIRouter, Depends
from sqlalchemy import Integer, cast, func, select

from app.api.deps import ContainerDep, Principal, PrincipalDep, SessionDep, requires
from app.core.exceptions import NotFoundError
from app.database.base import utcnow
from app.database.models import ModelRecord, ModelUsage
from app.models.providers.base import HealthStatus, ModelRole
from app.models.router import ModelRequirements
from app.schemas.common import ModelUpdate
from app.security.rbac import Permission

router = APIRouter(prefix="/models", tags=["models"])


async def _model_view(container: ContainerDep, model_id: str) -> dict[str, Any]:
    config = container.registry.get(model_id)
    if config is None:
        raise NotFoundError(f"Unknown model '{model_id}'.")
    report = await container.health.get(model_id)
    fresh = report is not None and container.health.is_fresh(report)
    status = report.status if report else HealthStatus.UNKNOWN
    return {
        **config.public_dict(),
        "status": str(status) if fresh or status in (HealthStatus.OFFLINE, HealthStatus.DISABLED) else "unknown",
        "online": bool(report and fresh and report.status == HealthStatus.ONLINE),
        "health": report.model_dump() if report else None,
        "health_fresh": fresh,
    }


@router.get("")
async def list_models(principal: PrincipalDep, container: ContainerDep) -> dict[str, Any]:
    return {"models": [await _model_view(container, m.id) for m in container.registry.all()],
            "warnings": [w.model_dump() for w in container.registry.warnings],
            "routing_policy": container.registry.policy.model_dump(mode="json")}


@router.get("/health")
async def models_health(principal: PrincipalDep, container: ContainerDep, refresh: bool = False) -> dict[str, Any]:
    """Real connectivity + functional checks. With refresh=true every model is probed now."""
    if refresh:
        reports = await container.health.check_all()
    else:
        reports = [await container.health.ensure_checked(m.id) for m in container.registry.all()]
    summary = {s: sum(1 for r in reports if r.status == s) for s in ("online", "degraded", "offline", "disabled")}
    return {"summary": summary, "models": [r.model_dump() for r in reports]}


@router.get("/usage")
async def usage(principal: PrincipalDep, session: SessionDep, days: int = 7) -> list[dict[str, Any]]:
    since = utcnow() - timedelta(days=min(days, 90))
    stmt = (select(ModelUsage.model_id, ModelUsage.operation, func.count(ModelUsage.id),
                   func.sum(cast(ModelUsage.success, Integer)), func.avg(ModelUsage.latency_ms),
                   func.sum(ModelUsage.prompt_tokens), func.sum(ModelUsage.completion_tokens))
            .where(ModelUsage.created_at >= since,
                   (ModelUsage.organization_id == principal.org_id) | ModelUsage.organization_id.is_(None))
            .group_by(ModelUsage.model_id, ModelUsage.operation))
    rows = (await session.execute(stmt)).all()
    return [{"model_id": r[0], "operation": r[1], "calls": r[2], "success_rate": round((r[3] or 0) / r[2], 3) if r[2] else None,
             "avg_latency_ms": int(r[4] or 0), "prompt_tokens": int(r[5] or 0), "completion_tokens": int(r[6] or 0)}
            for r in rows]


@router.get("/routing/preview")
async def routing_preview(principal: PrincipalDep, container: ContainerDep, role: ModelRole = ModelRole.CODING,
                          complexity: str = "simple", needs_tools: bool = False, min_context: int = 0) -> dict[str, Any]:
    req = ModelRequirements(role=role, complexity=complexity if complexity in ("trivial", "simple", "moderate", "complex")
                            else "simple", needs_tools=needs_tools, min_context=min_context)  # type: ignore[arg-type]
    ranked = await container.router.rank(req)
    try:
        decision = await container.router.select(req)
        chosen: dict[str, Any] | None = decision.model_dump()
    except Exception as exc:
        chosen = {"error": getattr(exc, "message", str(exc))}
    return {"requirements": req.model_dump(mode="json"), "decision": chosen, "candidates": [c.model_dump() for c in ranked]}


@router.get("/{model_id}")
async def get_model(model_id: str, principal: PrincipalDep, container: ContainerDep) -> dict[str, Any]:
    return await _model_view(container, model_id)


@router.post("/{model_id}/test")
async def test_model(model_id: str, principal: PrincipalDep, container: ContainerDep) -> dict[str, Any]:
    """Deep test: list models, verify the name, real completion with content check, and streaming."""
    report = await container.health.check(model_id, deep=True)
    return report.model_dump()


@router.patch("/{model_id}")
async def update_model(model_id: str, body: ModelUpdate, session: SessionDep, container: ContainerDep,
                       principal: Principal = Depends(requires(Permission.MODELS_MANAGE))) -> dict[str, Any]:
    record = await session.get(ModelRecord, model_id)
    if record is None or container.registry.get(model_id) is None:
        raise NotFoundError(f"Unknown model '{model_id}'.")
    if body.enabled is not None:
        record.enabled_override = body.enabled
    if body.priority is not None:
        record.priority_override = body.priority
    await session.flush()
    rows = (await session.execute(select(ModelRecord))).scalars().all()
    container.registry.apply_overrides({r.id: {"enabled": r.enabled_override, "priority": r.priority_override} for r in rows})
    await container.audit.record("model.updated", organization_id=principal.org_id, actor_id=principal.user_id,
                                 resource_type="model", resource_id=model_id,
                                 details=body.model_dump(exclude_none=True), session=session)
    return await _model_view(container, model_id)
