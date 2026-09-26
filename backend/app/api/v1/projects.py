from __future__ import annotations

import tempfile
import uuid
from pathlib import Path
from typing import Annotated, Any

from fastapi import APIRouter, Depends, File, Form, UploadFile
from sqlalchemy import select

from app.api.deps import ContainerDep, Principal, PrincipalDep, SessionDep, project_for, requires
from app.core.exceptions import ValidationFailed
from app.database.models import ChangeSet, IndexJob, Project, ProjectFile, Symbol
from app.schemas.common import GitImport, LocalImport, ProjectCreate, ProjectOut
from app.security.rbac import Permission
from app.services.projects import ProjectService
from app.tools.base import ProjectRef, ToolContext

router = APIRouter(prefix="/projects", tags=["projects"])
Writer = Annotated[Principal, Depends(requires(Permission.PROJECT_WRITE))]


def _job(job: IndexJob | None) -> dict[str, Any] | None:
    if job is None:
        return None
    return {"id": str(job.id), "status": job.status, "stats": job.stats, "warnings": job.warnings, "error": job.error,
            "started_at": job.started_at, "finished_at": job.finished_at}


@router.get("", response_model=list[ProjectOut])
async def list_projects(principal: PrincipalDep, session: SessionDep) -> list[Project]:
    stmt = select(Project).where(Project.organization_id == principal.org_id).order_by(Project.created_at.desc())
    return list((await session.execute(stmt)).scalars().all())


@router.post("", response_model=ProjectOut, status_code=201)
async def create_project(body: ProjectCreate, principal: Writer, session: SessionDep, container: ContainerDep) -> Project:
    project = await ProjectService(container).create_empty(session, principal.org, principal.user_id, body.name,
                                                           body.description)
    await container.audit.record("project.created", organization_id=principal.org_id, actor_id=principal.user_id,
                                 resource_type="project", resource_id=project.id)
    return project


@router.post("/import/upload", response_model=ProjectOut, status_code=201)
async def import_upload(principal: Writer, session: SessionDep, container: ContainerDep,
                        name: Annotated[str, Form()], file: Annotated[UploadFile, File()],
                        description: Annotated[str, Form()] = "") -> Project:
    if not (file.filename or "").lower().endswith(".zip"):
        raise ValidationFailed("Upload a .zip archive of your project.")
    limit = container.settings.max_upload_mb * 1024 * 1024
    with tempfile.NamedTemporaryFile(suffix=".zip", delete=False) as tmp:
        size = 0
        while chunk := await file.read(1024 * 1024):
            size += len(chunk)
            if size > limit:
                Path(tmp.name).unlink(missing_ok=True)
                raise ValidationFailed(f"The archive is larger than {container.settings.max_upload_mb} MB.")
            tmp.write(chunk)
        archive = Path(tmp.name)
    try:
        project = await ProjectService(container).import_zip(session, principal.org, principal.user_id, name, archive,
                                                             description)
    finally:
        archive.unlink(missing_ok=True)
    await container.audit.record("project.imported", organization_id=principal.org_id, actor_id=principal.user_id,
                                 resource_type="project", resource_id=project.id, details={"source": "upload"})
    return project


@router.post("/import/git", response_model=ProjectOut, status_code=201)
async def import_git(body: GitImport, principal: Writer, session: SessionDep, container: ContainerDep) -> Project:
    project = await ProjectService(container).import_git(session, principal.org, principal.user_id, body.name,
                                                         body.url, body.branch, body.description)
    await container.audit.record("project.imported", organization_id=principal.org_id, actor_id=principal.user_id,
                                 resource_type="project", resource_id=project.id,
                                 details={"source": "git", "url": project.source_ref})
    return project


@router.post("/import/local", response_model=ProjectOut, status_code=201)
async def import_local(body: LocalImport, principal: Writer, session: SessionDep, container: ContainerDep) -> Project:
    project = await ProjectService(container).import_local(session, principal.org, principal.user_id, body.name,
                                                           body.path, body.link, body.description)
    await container.audit.record("project.imported", organization_id=principal.org_id, actor_id=principal.user_id,
                                 resource_type="project", resource_id=project.id,
                                 details={"source": "local", "linked": body.link})
    return project


@router.get("/{project_id}")
async def get_project(project_id: uuid.UUID, principal: PrincipalDep, session: SessionDep,
                      container: ContainerDep) -> dict[str, Any]:
    project = await project_for(project_id, principal, session)
    job = await ProjectService(container).latest_job(session, project.id)
    return {**ProjectOut.model_validate(project).model_dump(mode="json"), "overview": project.overview,
            "index_job": _job(job)}


@router.delete("/{project_id}", status_code=204)
async def delete_project(project_id: uuid.UUID, session: SessionDep, container: ContainerDep,
                         principal: Principal = Depends(requires(Permission.PROJECT_DELETE))) -> None:
    project = await project_for(project_id, principal, session)
    await ProjectService(container).delete(session, project)
    await container.audit.record("project.deleted", organization_id=principal.org_id, actor_id=principal.user_id,
                                 resource_type="project", resource_id=project_id)


@router.post("/{project_id}/reindex", status_code=202)
async def reindex(project_id: uuid.UUID, principal: Writer, session: SessionDep, container: ContainerDep,
                  full: bool = False) -> dict[str, Any]:
    project = await project_for(project_id, principal, session)
    job = await ProjectService(container).enqueue_index(session, project, full=full)
    return {"job": _job(job)}


@router.get("/{project_id}/index-status")
async def index_status(project_id: uuid.UUID, principal: PrincipalDep, session: SessionDep,
                       container: ContainerDep) -> dict[str, Any]:
    project = await project_for(project_id, principal, session)
    job = await ProjectService(container).latest_job(session, project.id)
    return {"status": project.status, "message": project.status_message, "job": _job(job)}


@router.get("/{project_id}/overview")
async def overview(project_id: uuid.UUID, principal: PrincipalDep, session: SessionDep) -> dict[str, Any]:
    project = await project_for(project_id, principal, session)
    return project.overview or {}


@router.get("/{project_id}/symbols")
async def symbols(project_id: uuid.UUID, principal: PrincipalDep, session: SessionDep, q: str = "",
                  limit: int = 100) -> list[dict[str, Any]]:
    await project_for(project_id, principal, session)
    stmt = (select(Symbol, ProjectFile.path).join(ProjectFile, ProjectFile.id == Symbol.file_id)
            .where(Symbol.project_id == project_id))
    if q:
        stmt = stmt.where(Symbol.qualified_name.ilike(f"%{q[:100]}%"))
    rows = (await session.execute(stmt.order_by(Symbol.qualified_name).limit(min(limit, 500)))).all()
    return [{"name": s.qualified_name, "kind": s.kind, "file": path, "start_line": s.start_line,
             "end_line": s.end_line, "signature": s.signature} for s, path in rows]


@router.post("/{project_id}/scan")
async def scan(project_id: uuid.UUID, principal: PrincipalDep, session: SessionDep, container: ContainerDep) -> dict:
    project = await project_for(project_id, principal, session)
    ctx = ToolContext(org_id=principal.org_id, user_id=principal.user_id, role=principal.role,
                      services=container.tool_services,
                      project=ProjectRef(project.id, project.organization_id, project.name, Path(project.storage_path),
                                         project.overview or {}))
    outcome = await container.tools.invoke("scan_project", {}, ctx)
    return outcome.model_dump()


# --- change sets -----------------------------------------------------------------------------------------
@router.get("/{project_id}/changesets")
async def list_changesets(project_id: uuid.UUID, principal: PrincipalDep, session: SessionDep) -> list[dict[str, Any]]:
    await project_for(project_id, principal, session)
    rows = (await session.execute(select(ChangeSet).where(ChangeSet.project_id == project_id)
                                  .order_by(ChangeSet.created_at.desc()).limit(100))).scalars().all()
    return [{"id": str(c.id), "title": c.title, "status": c.status, "stats": c.stats, "run_id": str(c.run_id) if c.run_id else None,
             "created_at": c.created_at, "applied_at": c.applied_at} for c in rows]


@router.get("/{project_id}/changesets/{changeset_id}")
async def get_changeset(project_id: uuid.UUID, changeset_id: uuid.UUID, principal: PrincipalDep, session: SessionDep,
                        container: ContainerDep) -> dict[str, Any]:
    await project_for(project_id, principal, session)
    svc = container.tool_services.changesets
    cs = await svc.get(session, changeset_id, principal.org_id)
    files = await svc.files(session, cs.id)
    return {"id": str(cs.id), "title": cs.title, "summary": cs.summary, "status": cs.status, "stats": cs.stats,
            "run_id": str(cs.run_id) if cs.run_id else None,
            "files": [{"path": f.path, "operation": f.operation, "added": f.added_lines, "removed": f.removed_lines,
                       "diff": f.diff} for f in files]}


@router.post("/{project_id}/changesets/{changeset_id}/discard", status_code=204)
async def discard_changeset(project_id: uuid.UUID, changeset_id: uuid.UUID, principal: Writer, session: SessionDep,
                            container: ContainerDep) -> None:
    await project_for(project_id, principal, session)
    cs = await container.tool_services.changesets.get(session, changeset_id, principal.org_id)
    if cs.status == "open":
        cs.status = "discarded"
    await container.audit.record("changeset.discarded", organization_id=principal.org_id, actor_id=principal.user_id,
                                 resource_type="changeset", resource_id=changeset_id, session=session)
