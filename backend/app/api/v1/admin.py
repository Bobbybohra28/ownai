"""Administration and diagnostics.

``/admin/diagnostics/pipeline`` verifies every boundary of the AI pipeline with real
calls — so "the server pings but the AI answer is empty" problems are pinpointed to the
exact failing layer (endpoint, model name, completion content, parser, agent, orchestrator).
"""

from __future__ import annotations

import time
import uuid
from datetime import timedelta
from typing import Any

from fastapi import APIRouter, Depends
from sqlalchemy import func, select, text

from app.agents.base import AgentContext, AgentTask
from app.api.deps import ContainerDep, Principal, SessionDep, requires
from app.core.exceptions import AppError, ModelError, PermissionDenied
from app.database.base import utcnow
from app.database.models import AuditLog, ModelUsage, Task, TaskRun, TaskStatus, UsageRecord, User
from app.models.providers.base import ChatMessage, ChatRequest, ModelRole
from app.models.router import ModelRequirements
from app.orchestrator.evidence import EvidenceLedger
from app.security.rbac import Permission

router = APIRouter(prefix="/admin", tags=["admin"])
Admin = Depends(requires(Permission.AUDIT_READ))


async def service_health(container: ContainerDep) -> dict[str, Any]:
    checks: dict[str, Any] = {}
    started = time.perf_counter()
    try:
        async with container.sessions() as session:
            await session.execute(text("SELECT 1"))
        checks["database"] = {"ok": True, "latency_ms": int((time.perf_counter() - started) * 1000)}
    except Exception as exc:
        checks["database"] = {"ok": False, "error": type(exc).__name__}
    if container.redis is not None:
        try:
            await container.redis.ping()
            checks["redis"] = {"ok": True}
        except Exception as exc:
            checks["redis"] = {"ok": False, "error": type(exc).__name__}
    else:
        checks["redis"] = {"ok": True, "mode": "inline (not used)"}
    checks["qdrant"] = {"ok": await container.vector_store.health()}
    sandbox = await container.sandbox.health()
    checks["sandbox"] = {"ok": sandbox.get("status") == "ok", **sandbox}
    reports = [await container.health.get(m.id) for m in container.registry.all()]
    # same rule as the Models page: an old "online" result is not evidence the model is up now
    online = sum(1 for r in reports if r is not None and r.status == "online" and container.health.is_fresh(r))
    checks["models"] = {
        "ok": online > 0,
        "configured": len(reports),
        "online": online,
        "stale": sum(1 for r in reports if r is not None and not container.health.is_fresh(r)),
        "unchecked": sum(1 for r in reports if r is None),
    }
    return checks


@router.get("/health")
async def admin_health(container: ContainerDep, principal: Principal = Admin) -> dict[str, Any]:
    return await service_health(container)


@router.get("/audit-logs")
async def audit_logs(session: SessionDep, principal: Principal = Admin, action: str | None = None,
                     limit: int = 200) -> list[dict[str, Any]]:
    stmt = select(AuditLog).where(AuditLog.organization_id == principal.org_id)
    if action:
        stmt = stmt.where(AuditLog.action.startswith(action))
    rows = (await session.execute(stmt.order_by(AuditLog.created_at.desc()).limit(min(limit, 1000)))).scalars()
    return [{"id": r.id, "action": r.action, "actor_type": r.actor_type, "actor_id": r.actor_id,
             "resource_type": r.resource_type, "resource_id": r.resource_id, "outcome": r.outcome,
             "request_id": r.request_id, "details": r.details, "created_at": r.created_at} for r in rows]


@router.get("/usage")
async def usage(session: SessionDep, principal: Principal = Admin, days: int = 30) -> dict[str, Any]:
    since = utcnow() - timedelta(days=min(days, 365))
    records = (await session.execute(select(UsageRecord).where(UsageRecord.organization_id == principal.org_id))).scalars()
    runs = (await session.execute(select(TaskRun.status, func.count(TaskRun.id)).where(
        TaskRun.organization_id == principal.org_id, TaskRun.created_at >= since).group_by(TaskRun.status))).all()
    tokens = (await session.execute(select(func.sum(ModelUsage.prompt_tokens), func.sum(ModelUsage.completion_tokens))
                                    .where(ModelUsage.organization_id == principal.org_id,
                                           ModelUsage.created_at >= since))).one()
    return {"usage_records": [{"period": r.period, "metric": r.metric, "value": r.value} for r in records],
            "runs_by_status": {s: c for s, c in runs},
            "tokens": {"prompt": int(tokens[0] or 0), "completion": int(tokens[1] or 0)}}


@router.get("/users")
async def users(session: SessionDep, principal: Principal = Admin) -> list[dict[str, Any]]:
    if not principal.user.is_superadmin:
        raise PermissionDenied("Only the installation administrator can list all users.")
    rows = (await session.execute(select(User).order_by(User.created_at))).scalars()
    return [{"id": str(u.id), "email": u.email, "display_name": u.display_name, "is_active": u.is_active,
             "is_superadmin": u.is_superadmin, "last_login_at": u.last_login_at} for u in rows]


def _step(name: str, ok: bool, started: float, **extra: Any) -> dict[str, Any]:
    return {"step": name, "ok": ok, "latency_ms": int((time.perf_counter() - started) * 1000), **extra}


@router.post("/diagnostics/pipeline")
async def pipeline(container: ContainerDep, principal: Principal = Admin, full: bool = False) -> dict[str, Any]:
    """Real end-to-end probe of every boundary. ``full=true`` also runs a complete orchestrator task."""
    steps: list[dict[str, Any]] = []
    services = await service_health(container)
    started = time.perf_counter()
    steps.append(_step("services", all(v.get("ok") for k, v in services.items() if k != "models"), started,
                       detail=services))
    if container.registry.warnings:
        steps.append({"step": "model_config", "ok": bool(container.registry.chat_models()), "latency_ms": 0,
                      "warnings": [w.model_dump() for w in container.registry.warnings]})
    for model in container.registry.all():
        started = time.perf_counter()
        report = await container.health.check(model.id, deep=True)
        steps.append(_step(f"model:{model.id}", report.status == "online", started, status=str(report.status),
                           error_code=report.error_code, message=report.error,
                           checks=[s.model_dump() for s in report.steps]))
    # router → provider → parser
    started = time.perf_counter()
    try:
        routed = await container.router.chat(
            ChatRequest(messages=[ChatMessage(role="user", content="Reply with exactly one word: PONG")],
                        max_tokens=512, temperature=0.0),
            ModelRequirements(role=ModelRole.FAST, complexity="trivial", agent_id="diagnostics"),
        )
        content = routed.response.content
        steps.append(_step("router_provider_parser", "pong" in content.lower(), started,
                           model_id=routed.decision.model_id, reason=routed.decision.reason,
                           content_preview=content[:80], content_chars=len(content),
                           message=None if "pong" in content.lower() else "Model answered but did not follow the instruction."))
    except ModelError as exc:
        steps.append(_step("router_provider_parser", False, started, error_code=str(exc.code), message=exc.message,
                           hint=exc.hint))
    # agent layer
    started = time.perf_counter()
    try:
        ctx = AgentContext(run_id=None, org_id=principal.org_id, user_id=principal.user_id, role=principal.role,
                           settings=container.settings, router=container.router, tools=container.tools,
                           tool_registry=container.tool_registry, tool_services=container.tool_services, events=None,
                           evidence=EvidenceLedger())
        result = await container.agents.get("assistant").run(
            AgentTask(step_id="diag", goal="Reply with one short sentence confirming you work.",
                      request="Reply with one short sentence confirming you work.", complexity="trivial"), ctx)
        steps.append(_step("agent", result.status == "succeeded" and bool(result.text), started, status=result.status,
                           model_ids=result.model_ids, text_chars=len(result.text), error_code=result.error_code,
                           message=result.error_message))
    except AppError as exc:
        steps.append(_step("agent", False, started, error_code=str(exc.code), message=exc.message))
    if full:
        started = time.perf_counter()
        async with container.sessions() as session:
            task = Task(organization_id=principal.org_id, user_id=principal.user_id, mode="quick",
                        request="Diagnostics: answer with one short sentence that says the pipeline works.")
            session.add(task)
            await session.flush()
            run = TaskRun(organization_id=principal.org_id, task_id=task.id, status=TaskStatus.QUEUED.value)
            session.add(run)
            await session.commit()
            run_id = run.id
        await container.orchestrator.start(run_id)
        async with container.sessions() as session:
            run = await session.get(TaskRun, run_id)
            assert run is not None
            answer = (run.result or {}).get("answer", "")
            steps.append(_step("orchestrator", run.status == TaskStatus.SUCCEEDED.value and bool(answer), started,
                               run_id=str(run_id), status=run.status, answer_chars=len(answer),
                               error_code=run.error_code, message=run.error_message))
    first_failure = next((s for s in steps if not s["ok"]), None)
    return {"ok": first_failure is None, "first_failure": first_failure["step"] if first_failure else None,
            "steps": steps, "checked_at": utcnow().isoformat(), "id": str(uuid.uuid4())}
