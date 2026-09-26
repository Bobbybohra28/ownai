from __future__ import annotations

import uuid
from typing import Any

from fastapi import APIRouter
from sqlalchemy import select

from app.api.deps import ContainerDep, Principal, PrincipalDep, SessionDep, project_for
from app.core.exceptions import NotFoundError, ValidationFailed
from app.database.models import Conversation, Message
from app.database.repositories.base import get_scoped
from app.orchestrator.modes import Mode
from app.schemas.common import ConversationCreate, ConversationOut, ConversationUpdate, MessageCreate, MessageOut
from app.services.runs import RunService

router = APIRouter(prefix="/conversations", tags=["conversations"])


async def _conversation(conversation_id: uuid.UUID, principal: Principal, session: SessionDep) -> Conversation:
    conversation = await get_scoped(session, Conversation, conversation_id, principal.org_id, label="Conversation")
    if conversation.user_id != principal.user_id:
        # conversations are private to their author (projects are shared, chats are not)
        raise NotFoundError("Conversation not found.")
    return conversation


def _validate_mode(mode: str | None) -> None:
    if mode is not None and mode not in {m.value for m in Mode}:
        raise ValidationFailed(f"Unknown mode '{mode}'. Use one of: {', '.join(m.value for m in Mode)}.")


@router.get("", response_model=list[ConversationOut])
async def list_conversations(principal: PrincipalDep, session: SessionDep, project_id: uuid.UUID | None = None,
                             archived: bool = False) -> list[Conversation]:
    stmt = select(Conversation).where(Conversation.organization_id == principal.org_id,
                                      Conversation.user_id == principal.user_id, Conversation.archived.is_(archived))
    if project_id:
        stmt = stmt.where(Conversation.project_id == project_id)
    return list((await session.execute(stmt.order_by(Conversation.updated_at.desc()).limit(200))).scalars().all())


@router.post("", response_model=ConversationOut, status_code=201)
async def create_conversation(body: ConversationCreate, principal: PrincipalDep, session: SessionDep) -> Conversation:
    _validate_mode(body.mode)
    if body.project_id:
        await project_for(body.project_id, principal, session)
    conversation = Conversation(organization_id=principal.org_id, user_id=principal.user_id, project_id=body.project_id,
                                title=body.title or "New conversation", mode=body.mode)
    session.add(conversation)
    await session.flush()
    await session.refresh(conversation)
    return conversation


@router.get("/{conversation_id}", response_model=ConversationOut)
async def get_conversation(conversation_id: uuid.UUID, principal: PrincipalDep, session: SessionDep) -> Conversation:
    return await _conversation(conversation_id, principal, session)


@router.patch("/{conversation_id}", response_model=ConversationOut)
async def update_conversation(conversation_id: uuid.UUID, body: ConversationUpdate, principal: PrincipalDep,
                              session: SessionDep) -> Conversation:
    conversation = await _conversation(conversation_id, principal, session)
    _validate_mode(body.mode)
    if body.title is not None:
        conversation.title = body.title
    if body.mode is not None:
        conversation.mode = body.mode
    if body.archived is not None:
        conversation.archived = body.archived
    if "project_id" in body.model_fields_set:
        if body.project_id:
            await project_for(body.project_id, principal, session)
        conversation.project_id = body.project_id
    await session.flush()
    await session.refresh(conversation)
    return conversation


@router.delete("/{conversation_id}", status_code=204)
async def delete_conversation(conversation_id: uuid.UUID, principal: PrincipalDep, session: SessionDep) -> None:
    await session.delete(await _conversation(conversation_id, principal, session))


@router.get("/{conversation_id}/messages", response_model=list[MessageOut])
async def list_messages(conversation_id: uuid.UUID, principal: PrincipalDep, session: SessionDep) -> list[Message]:
    await _conversation(conversation_id, principal, session)
    stmt = select(Message).where(Message.conversation_id == conversation_id).order_by(Message.created_at)
    return list((await session.execute(stmt.limit(1000))).scalars().all())


@router.post("/{conversation_id}/messages", status_code=202)
async def send_message(conversation_id: uuid.UUID, body: MessageCreate, principal: PrincipalDep, session: SessionDep,
                       container: ContainerDep) -> dict[str, Any]:
    conversation = await _conversation(conversation_id, principal, session)
    session.expunge(conversation)
    await session.commit()
    return await RunService(container).submit(org=principal.org, user_id=principal.user_id, role=principal.role,
                                              conversation=conversation, content=body.content, mode=body.mode)
