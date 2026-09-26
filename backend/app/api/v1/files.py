"""Project files and uploaded documents."""

from __future__ import annotations

import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, File, Form, UploadFile
from sqlalchemy import select

from app.api.deps import ContainerDep, Principal, PrincipalDep, SessionDep, project_for, requires
from app.core.exceptions import ValidationFailed
from app.database.models import Document
from app.database.repositories.base import get_scoped
from app.security.rbac import Permission
from app.services.documents import delete_document, store_document
from app.services.projects import ProjectService

router = APIRouter(tags=["files"])


@router.get("/projects/{project_id}/files")
async def file_tree(project_id: uuid.UUID, principal: PrincipalDep, session: SessionDep,
                    container: ContainerDep) -> dict[str, Any]:
    project = await project_for(project_id, principal, session)
    return await ProjectService(container).tree(session, project)


@router.get("/projects/{project_id}/files/content")
async def file_content(project_id: uuid.UUID, path: str, principal: PrincipalDep, session: SessionDep,
                       container: ContainerDep) -> dict[str, Any]:
    project = await project_for(project_id, principal, session)
    return ProjectService(container).read_file(project, path)


def _doc(d: Document) -> dict[str, Any]:
    return {"id": str(d.id), "title": d.title, "doc_type": d.doc_type, "status": d.status, "error": d.error,
            "project_id": str(d.project_id) if d.project_id else None, "meta": d.meta, "created_at": d.created_at}


@router.get("/documents")
async def list_documents(principal: PrincipalDep, session: SessionDep,
                         project_id: uuid.UUID | None = None) -> list[dict[str, Any]]:
    stmt = select(Document).where(Document.organization_id == principal.org_id)
    if project_id:
        stmt = stmt.where(Document.project_id == project_id)
    rows = (await session.execute(stmt.order_by(Document.created_at.desc()).limit(500))).scalars().all()
    return [_doc(d) for d in rows]


@router.post("/documents", status_code=201)
async def upload_document(session: SessionDep, container: ContainerDep, file: Annotated[UploadFile, File()],
                          project_id: Annotated[str | None, Form()] = None, title: Annotated[str | None, Form()] = None,
                          principal: Principal = Depends(requires(Permission.PROJECT_WRITE))) -> dict[str, Any]:
    pid = None
    if project_id:
        pid = (await project_for(uuid.UUID(project_id), principal, session)).id
    data = await file.read(container.settings.max_upload_mb * 1024 * 1024 + 1)
    if not data:
        raise ValidationFailed("The uploaded file is empty.")
    document = await store_document(container, org_id=principal.org_id, project_id=pid, user_id=principal.user_id,
                                    filename=file.filename or "document", data=data, title=title)
    await container.audit.record("document.uploaded", organization_id=principal.org_id, actor_id=principal.user_id,
                                 resource_type="document", resource_id=document.id)
    return _doc(document)


@router.get("/documents/{document_id}")
async def get_document(document_id: uuid.UUID, principal: PrincipalDep, session: SessionDep) -> dict[str, Any]:
    return _doc(await get_scoped(session, Document, document_id, principal.org_id, label="Document"))


@router.delete("/documents/{document_id}", status_code=204)
async def remove_document(document_id: uuid.UUID, session: SessionDep, container: ContainerDep,
                          principal: Principal = Depends(requires(Permission.PROJECT_WRITE))) -> None:
    document = await get_scoped(session, Document, document_id, principal.org_id, label="Document")
    await delete_document(container, document)
    await container.audit.record("document.deleted", organization_id=principal.org_id, actor_id=principal.user_id,
                                 resource_type="document", resource_id=document_id)
