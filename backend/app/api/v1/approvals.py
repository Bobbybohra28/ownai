"""Human approval of risky actions (applying code changes, write SQL, commits, …)."""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any

from fastapi import APIRouter
from sqlalchemy import select, update

from app.api.deps import ContainerDep, PrincipalDep, SessionDep
from app.database.models import Approval, ApprovalStatus, ChangeSet, Project
from app.database.repositories.base import get_scoped
from app.schemas.common import ApprovalDecision
from app.services.runs import RunService
from app.tools.base import ProjectRef, ToolContext

router = APIRouter(prefix="/approvals", tags=["approvals"])


async def _view(session: SessionDep, a: Approval) -> dict[str, Any]:
    data: dict[str, Any] = {
        "id": str(a.id), "kind": a.kind, "tool": a.tool_name, "title": a.title, "description": a.description,
        "risk_level": a.risk_level, "status": a.status, "args": a.args, "run_id": str(a.run_id) if a.run_id else None,
        "project_id": str(a.project_id) if a.project_id else None, "created_at": a.created_at,
        "decided_at": a.decided_at, "result": a.result, "expires_at": a.expires_at,
    }
    if a.tool_name == "apply_changeset" and a.args.get("changeset_id"):
        cs = await session.get(ChangeSet, uuid.UUID(a.args["changeset_id"]))
        if cs is not None:
            data["changeset"] = {"id": str(cs.id), "status": cs.status, "stats": cs.stats, "title": cs.title}
    return data


@router.get("")
async def list_approvals(principal: PrincipalDep, session: SessionDep, status: str | None = "pending",
                         limit: int = 100) -> list[dict[str, Any]]:
    stmt = select(Approval).where(Approval.organization_id == principal.org_id)
    if status:
        stmt = stmt.where(Approval.status == status)
    rows = (await session.execute(stmt.order_by(Approval.created_at.desc()).limit(min(limit, 500)))).scalars().all()
    return [await _view(session, a) for a in rows]


@router.get("/{approval_id}")
async def get_approval(approval_id: uuid.UUID, principal: PrincipalDep, session: SessionDep) -> dict[str, Any]:
    return await _view(session, await get_scoped(session, Approval, approval_id, principal.org_id, label="Approval"))


async def _execute_standalone(container: ContainerDep, principal: PrincipalDep, approval: Approval) -> dict[str, Any]:
    """Approvals created outside an agent run (e.g. from the SQL or Git pages) execute immediately."""
    project_ref = None
    async with container.sessions() as session:
        if approval.project_id:
            project = await session.get(Project, approval.project_id)
            if project is not None:
                project_ref = ProjectRef(project.id, project.organization_id, project.name, Path(project.storage_path),
                                         project.overview or {})
    ctx = ToolContext(org_id=principal.org_id, user_id=principal.user_id, role=principal.role,
                      services=container.tool_services, project=project_ref, approved=True, approval_id=approval.id)
    outcome = await container.tools.invoke(approval.tool_name or "", dict(approval.args), ctx)
    async with container.sessions() as session:
        await session.execute(update(Approval).where(Approval.id == approval.id).values(
            status=ApprovalStatus.EXECUTED.value if outcome.ok else ApprovalStatus.FAILED.value,
            result={"status": outcome.status, "summary": outcome.summary, "data": outcome.data,
                    "tool_run_id": outcome.tool_run_id}))
        await session.commit()
    return outcome.model_dump()


async def _decide(approval_id: uuid.UUID, approve: bool, body: ApprovalDecision, principal: PrincipalDep,
                  session: SessionDep, container: ContainerDep) -> dict[str, Any]:
    await get_scoped(session, Approval, approval_id, principal.org_id, label="Approval")
    await session.commit()
    approval = await RunService(container).decide(approval_id, org_id=principal.org_id, user_id=principal.user_id,
                                                  role=principal.role, approve=approve, note=body.note)
    result: dict[str, Any] = {"id": str(approval.id), "status": approval.status}
    if approve and approval.run_id is None:
        result["execution"] = await _execute_standalone(container, principal, approval)
    elif approval.run_id:
        result["run_id"] = str(approval.run_id)
        result["message"] = "The task will continue now."
    return result


@router.post("/{approval_id}/approve")
async def approve(approval_id: uuid.UUID, body: ApprovalDecision, principal: PrincipalDep, session: SessionDep,
                  container: ContainerDep) -> dict[str, Any]:
    return await _decide(approval_id, True, body, principal, session, container)


@router.post("/{approval_id}/reject")
async def reject(approval_id: uuid.UUID, body: ApprovalDecision, principal: PrincipalDep, session: SessionDep,
                 container: ContainerDep) -> dict[str, Any]:
    return await _decide(approval_id, False, body, principal, session, container)
