"""Agent catalogue, enable/disable, and the agent activity monitor."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends
from sqlalchemy import select

from app.api.deps import ContainerDep, Principal, PrincipalDep, SessionDep, requires
from app.database.models import AgentRecord, RunStep, Task, TaskRun
from app.schemas.common import AgentUpdate
from app.security.rbac import Permission

router = APIRouter(prefix="/agents", tags=["agents"])


def _spec_view(spec: Any) -> dict[str, Any]:
    data = spec.model_dump(mode="json")
    data["system_prompt"] = (data.get("system_prompt") or "")[:600]
    return data


@router.get("")
async def list_agents(principal: PrincipalDep, container: ContainerDep) -> list[dict[str, Any]]:
    return [_spec_view(s) for s in container.agents.specs()]


@router.get("/activity")
async def activity(principal: PrincipalDep, session: SessionDep, limit: int = 100) -> list[dict[str, Any]]:
    """Agent monitor: recent agent executions with model, task, duration, tools, result and errors."""
    stmt = (select(RunStep, Task.request, TaskRun.id).join(TaskRun, TaskRun.id == RunStep.run_id)
            .join(Task, Task.id == TaskRun.task_id).where(TaskRun.organization_id == principal.org_id)
            .order_by(RunStep.started_at.desc().nulls_last()).limit(min(limit, 500)))
    rows = (await session.execute(stmt)).all()
    out = []
    for step, request, run_id in rows:
        duration = int((step.finished_at - step.started_at).total_seconds() * 1000) if step.finished_at and step.started_at else None
        out.append({"agent": step.agent_id, "status": step.status, "model": step.model_id, "task": request[:200],
                    "run_id": str(run_id), "step": step.step_key, "duration_ms": duration,
                    "tools": [t.get("tool") for t in step.tools_used or []], "result": step.summary[:300],
                    "error": step.error_message, "started_at": step.started_at})
    return out


@router.get("/{agent_id}")
async def get_agent(agent_id: str, principal: PrincipalDep, container: ContainerDep) -> dict[str, Any]:
    return _spec_view(container.agents.spec(agent_id))


@router.patch("/{agent_id}")
async def update_agent(agent_id: str, body: AgentUpdate, session: SessionDep, container: ContainerDep,
                       principal: Principal = Depends(requires(Permission.AGENTS_MANAGE))) -> dict[str, Any]:
    container.agents.spec(agent_id)  # 404 for unknown agents
    record = await session.get(AgentRecord, agent_id)
    if record is not None:
        record.enabled_override = body.enabled
    await session.flush()
    rows = (await session.execute(select(AgentRecord))).scalars().all()
    container.agents.apply_overrides({r.id: r.enabled_override for r in rows if r.enabled_override is not None})
    await container.audit.record("agent.updated", organization_id=principal.org_id, actor_id=principal.user_id,
                                 resource_type="agent", resource_id=agent_id, details={"enabled": body.enabled},
                                 session=session)
    return _spec_view(container.agents.spec(agent_id))
