"""User and project memory management (secrets are rejected on write)."""

from __future__ import annotations

import uuid
from typing import Any

from fastapi import APIRouter

from app.api.deps import PrincipalDep, SessionDep, project_for
from app.core.exceptions import NotFoundError, ValidationFailed
from app.database.models import Memory
from app.memory.long_term import MemoryStore, check_storable
from app.schemas.common import MemoryCreate, MemoryUpdate

router = APIRouter(prefix="/memory", tags=["memory"])


def _view(m: Memory) -> dict[str, Any]:
    return {"id": str(m.id), "scope": m.scope, "kind": m.kind, "content": m.content, "source": m.source,
            "confirmed": m.confirmed, "importance": m.importance,
            "project_id": str(m.project_id) if m.project_id else None, "created_at": m.created_at}


def _visible(m: Memory, user_id: uuid.UUID) -> bool:
    return m.scope != "user" or m.user_id == user_id


@router.get("")
async def list_memories(principal: PrincipalDep, session: SessionDep, scope: str | None = None,
                        project_id: uuid.UUID | None = None) -> list[dict[str, Any]]:
    rows = await MemoryStore(session).list(principal.org_id, scope=scope, user_id=principal.user_id,
                                           project_id=project_id)
    return [_view(m) for m in rows if _visible(m, principal.user_id)]


@router.post("", status_code=201)
async def create_memory(body: MemoryCreate, principal: PrincipalDep, session: SessionDep) -> dict[str, Any]:
    if body.scope == "project":
        if not body.project_id:
            raise ValidationFailed("project_id is required for project memory.")
        await project_for(body.project_id, principal, session)
    memory = await MemoryStore(session).add(
        org_id=principal.org_id, scope=body.scope, kind=body.kind, content=body.content,
        user_id=principal.user_id if body.scope == "user" else None,
        project_id=body.project_id if body.scope == "project" else None, source="user",
    )
    return _view(memory)


@router.patch("/{memory_id}")
async def update_memory(memory_id: uuid.UUID, body: MemoryUpdate, principal: PrincipalDep,
                        session: SessionDep) -> dict[str, Any]:
    memory = await MemoryStore(session).get(memory_id, principal.org_id)
    if not _visible(memory, principal.user_id):
        raise NotFoundError("Memory not found.")
    if body.content is not None:
        check_storable(body.content)
        memory.content = body.content.strip()
    if body.confirmed is not None:
        memory.confirmed = body.confirmed
    if body.importance is not None:
        memory.importance = body.importance
    await session.flush()
    return _view(memory)


@router.delete("/{memory_id}", status_code=204)
async def delete_memory(memory_id: uuid.UUID, principal: PrincipalDep, session: SessionDep) -> None:
    store = MemoryStore(session)
    memory = await store.get(memory_id, principal.org_id)
    if not _visible(memory, principal.user_id):
        raise NotFoundError("Memory not found.")
    await store.delete(memory)
