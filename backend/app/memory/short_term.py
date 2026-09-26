"""Short-term (session) memory: the recent conversation window within a token budget."""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import Message
from app.models.tokens import estimate_tokens


async def recent_turns(session: AsyncSession, conversation_id: uuid.UUID, *, exclude_message_id: uuid.UUID | None,
                       budget_tokens: int = 2500, max_messages: int = 12) -> list[dict[str, str]]:
    stmt = (select(Message).where(Message.conversation_id == conversation_id, Message.role.in_(["user", "assistant"]))
            .order_by(Message.created_at.desc()).limit(max_messages + 1))
    rows = [m for m in (await session.execute(stmt)).scalars().all() if m.id != exclude_message_id]
    turns: list[dict[str, str]] = []
    used = 0
    for message in rows[:max_messages]:
        content = message.content
        cost = estimate_tokens(content)
        if used + cost > budget_tokens:
            if not turns:
                content = content[: int(budget_tokens * 3)]
                turns.append({"role": message.role, "content": content})
            break
        used += cost
        turns.append({"role": message.role, "content": content})
    return list(reversed(turns))
