"""Tool catalogue and direct (user-initiated) tool execution through the same secured executor."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import APIRouter

from app.api.deps import ContainerDep, PrincipalDep, SessionDep, project_for
from app.schemas.common import ToolInvokeRequest
from app.tools.base import ProjectRef, ToolContext

router = APIRouter(prefix="/tools", tags=["tools"])


@router.get("")
async def list_tools(principal: PrincipalDep, container: ContainerDep) -> list[dict[str, Any]]:
    return container.tool_registry.describe()


@router.post("/{tool_name}/invoke")
async def invoke(tool_name: str, body: ToolInvokeRequest, principal: PrincipalDep, session: SessionDep,
                 container: ContainerDep) -> dict[str, Any]:
    project_ref = None
    if body.project_id:
        project = await project_for(body.project_id, principal, session)
        project_ref = ProjectRef(project.id, project.organization_id, project.name, Path(project.storage_path),
                                 project.overview or {})
    await session.commit()
    ctx = ToolContext(org_id=principal.org_id, user_id=principal.user_id, role=principal.role,
                      services=container.tool_services, project=project_ref)
    outcome = await container.tools.invoke(tool_name, body.args, ctx)
    return outcome.model_dump()
