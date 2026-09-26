"""Git endpoints — all go through the audited tool executor; commit/checkout create approvals."""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any

from fastapi import APIRouter

from app.api.deps import ContainerDep, PrincipalDep, SessionDep, project_for
from app.schemas.common import GitBranchRequest, GitCommitRequest
from app.tools.base import ProjectRef, ToolContext

router = APIRouter(prefix="/projects/{project_id}/git", tags=["git"])


async def _call(tool: str, args: dict[str, Any], project_id: uuid.UUID, principal: PrincipalDep, session: SessionDep,
                container: ContainerDep) -> dict[str, Any]:
    project = await project_for(project_id, principal, session)
    await session.commit()
    ctx = ToolContext(org_id=principal.org_id, user_id=principal.user_id, role=principal.role,
                      services=container.tool_services,
                      project=ProjectRef(project.id, project.organization_id, project.name, Path(project.storage_path),
                                         project.overview or {}))
    return (await container.tools.invoke(tool, args, ctx)).model_dump()


@router.get("/status")
async def status(project_id: uuid.UUID, principal: PrincipalDep, session: SessionDep, container: ContainerDep) -> dict:
    return await _call("git_status", {}, project_id, principal, session, container)


@router.get("/diff")
async def diff(project_id: uuid.UUID, principal: PrincipalDep, session: SessionDep, container: ContainerDep,
               path: str | None = None, staged: bool = False, ref: str | None = None) -> dict:
    args: dict[str, Any] = {"staged": staged}
    if path:
        args["path"] = path
    if ref:
        args["ref"] = ref
    return await _call("git_diff", args, project_id, principal, session, container)


@router.get("/log")
async def log(project_id: uuid.UUID, principal: PrincipalDep, session: SessionDep, container: ContainerDep,
              limit: int = 30, path: str | None = None) -> dict:
    args: dict[str, Any] = {"limit": min(limit, 200)}
    if path:
        args["path"] = path
    return await _call("git_log", args, project_id, principal, session, container)


@router.get("/branches")
async def branches(project_id: uuid.UUID, principal: PrincipalDep, session: SessionDep, container: ContainerDep) -> dict:
    return await _call("git_branch", {}, project_id, principal, session, container)


@router.post("/branches")
async def create_branch(project_id: uuid.UUID, body: GitBranchRequest, principal: PrincipalDep, session: SessionDep,
                        container: ContainerDep) -> dict:
    return await _call("git_branch", {"create": body.name}, project_id, principal, session, container)


@router.post("/checkout")
async def checkout(project_id: uuid.UUID, body: GitBranchRequest, principal: PrincipalDep, session: SessionDep,
                   container: ContainerDep) -> dict:
    """Creates an approval request; the checkout runs once approved."""
    return await _call("git_checkout", {"branch": body.name}, project_id, principal, session, container)


@router.post("/commit")
async def commit(project_id: uuid.UUID, body: GitCommitRequest, principal: PrincipalDep, session: SessionDep,
                 container: ContainerDep) -> dict:
    """Creates an approval request; the commit runs once approved. Never pushes."""
    return await _call("git_commit", body.model_dump(), project_id, principal, session, container)
