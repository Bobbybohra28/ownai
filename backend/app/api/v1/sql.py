"""Database connections (credentials encrypted at rest), schema inspection, queries, validation."""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends
from sqlalchemy import select

from app.api.deps import ContainerDep, Principal, PrincipalDep, SessionDep, project_for, requires
from app.core.exceptions import ConfigurationError, ValidationFailed
from app.database.models import DbConnection
from app.database.repositories.base import get_scoped
from app.schemas.common import DbConnectionCreate, SQLQueryRequest, SQLValidateRequest
from app.security.paths import PathJail
from app.security.rbac import Permission
from app.tools.base import ProjectRef, ToolContext
from app.tools.sql import classify_sql

router = APIRouter(tags=["sql"])


def _view(c: DbConnection) -> dict[str, Any]:
    return {"id": str(c.id), "name": c.name, "dialect": c.dialect, "host": c.host, "port": c.port,
            "database": c.database, "username": c.username, "read_only": c.read_only,
            "has_password": c.secret_ciphertext is not None, "project_id": str(c.project_id), "created_at": c.created_at}


@router.get("/projects/{project_id}/db-connections")
async def list_connections(project_id: uuid.UUID, principal: PrincipalDep, session: SessionDep) -> list[dict[str, Any]]:
    await project_for(project_id, principal, session)
    rows = (await session.execute(select(DbConnection).where(DbConnection.project_id == project_id,
                                                             DbConnection.organization_id == principal.org_id))).scalars()
    return [_view(c) for c in rows]


@router.post("/projects/{project_id}/db-connections", status_code=201)
async def create_connection(project_id: uuid.UUID, body: DbConnectionCreate, session: SessionDep,
                            container: ContainerDep,
                            principal: Principal = Depends(requires(Permission.SQL_CONNECT))) -> dict[str, Any]:
    project = await project_for(project_id, principal, session)
    if body.dialect == "sqlite":
        PathJail(Path(project.storage_path)).resolve(body.database, must_exist=True)
    elif not body.host:
        raise ValidationFailed("host is required for PostgreSQL connections.")
    ciphertext = None
    if body.password:
        if container.secret_box is None:
            raise ConfigurationError("OWNAI_ENCRYPTION_KEY is not configured; cannot store database passwords.")
        ciphertext = container.secret_box.encrypt(body.password)
    connection = DbConnection(organization_id=principal.org_id, project_id=project.id, name=body.name,
                              dialect=body.dialect, host=body.host, port=body.port, database=body.database,
                              username=body.username, secret_ciphertext=ciphertext, read_only=body.read_only,
                              created_by=principal.user_id)
    session.add(connection)
    await session.flush()
    await container.audit.record("db_connection.created", organization_id=principal.org_id, actor_id=principal.user_id,
                                 resource_type="db_connection", resource_id=connection.id,
                                 details={"dialect": body.dialect, "read_only": body.read_only}, session=session)
    return _view(connection)


@router.delete("/db-connections/{connection_id}", status_code=204)
async def delete_connection(connection_id: uuid.UUID, session: SessionDep, container: ContainerDep,
                            principal: Principal = Depends(requires(Permission.SQL_CONNECT))) -> None:
    connection = await get_scoped(session, DbConnection, connection_id, principal.org_id, label="Connection")
    await session.delete(connection)
    await container.audit.record("db_connection.deleted", organization_id=principal.org_id, actor_id=principal.user_id,
                                 resource_type="db_connection", resource_id=connection_id, session=session)


async def _tool(tool: str, args: dict[str, Any], connection_id: uuid.UUID, principal: PrincipalDep,
                session: SessionDep, container: ContainerDep) -> dict[str, Any]:
    connection = await get_scoped(session, DbConnection, connection_id, principal.org_id, label="Connection")
    project = await project_for(connection.project_id, principal, session)
    await session.commit()
    ctx = ToolContext(org_id=principal.org_id, user_id=principal.user_id, role=principal.role,
                      services=container.tool_services,
                      project=ProjectRef(project.id, project.organization_id, project.name, Path(project.storage_path),
                                         project.overview or {}))
    return (await container.tools.invoke(tool, {"connection_id": str(connection_id), **args}, ctx)).model_dump()


@router.get("/db-connections/{connection_id}/schema")
async def schema(connection_id: uuid.UUID, principal: PrincipalDep, session: SessionDep, container: ContainerDep) -> dict:
    return await _tool("inspect_database", {}, connection_id, principal, session, container)


@router.post("/db-connections/{connection_id}/query")
async def query(connection_id: uuid.UUID, body: SQLQueryRequest, principal: PrincipalDep, session: SessionDep,
                container: ContainerDep) -> dict:
    """Read-only statements run immediately; anything else returns an approval request."""
    return await _tool("run_sql", {"query": body.query, "max_rows": body.max_rows}, connection_id, principal, session,
                       container)


@router.post("/sql/validate")
async def validate(body: SQLValidateRequest, principal: PrincipalDep) -> dict[str, Any]:
    c = classify_sql(body.query, body.dialect)
    return {"valid": c.parse_error is None, "category": c.category, "warnings": c.warnings,
            "requires_approval": not c.is_read_only, "parse_error": c.parse_error, "normalized": c.statements}
