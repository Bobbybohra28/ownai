"""MemoryManager: one façade over short-term, conversation, project, user, task and agent memory."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.exceptions import ValidationFailed
from app.core.logging import get_logger
from app.database.models import Conversation
from app.memory.long_term import MemoryStore
from app.memory.short_term import recent_turns
from app.memory.summarizer import maybe_summarize
from app.models.router import ModelRouter

log = get_logger(__name__)


@dataclass
class TaskMemory:
    history: list[dict[str, str]] = field(default_factory=list)
    text: str = ""
    memory_ids: list[str] = field(default_factory=list)


class MemoryManager:
    def __init__(self, sessions: async_sessionmaker[AsyncSession], router: ModelRouter) -> None:
        self.sessions = sessions
        self.router = router

    async def for_task(self, *, org_id: uuid.UUID, user_id: uuid.UUID, project_id: uuid.UUID | None,
                       conversation_id: uuid.UUID | None, message_id: uuid.UUID | None, query: str) -> TaskMemory:
        result = TaskMemory()
        async with self.sessions() as session:
            summary = ""
            if conversation_id:
                conversation = await session.get(Conversation, conversation_id)
                if conversation is not None:
                    if await maybe_summarize(session, conversation, self.router):
                        await session.commit()
                    summary = conversation.summary
                result.history = await recent_turns(session, conversation_id, exclude_message_id=message_id)
            memories = await MemoryStore(session).relevant(org_id, query, user_id=user_id, project_id=project_id)
        lines = []
        if summary:
            lines.append(f"Earlier in this conversation: {summary}")
        for m in memories:
            lines.append(f"- ({m.scope}/{m.kind}) {m.content}")
            result.memory_ids.append(str(m.id))
        result.text = "\n".join(lines)
        return result

    async def remember(self, **kwargs: object) -> None:
        """Best-effort write used by the orchestrator (task/project memories). Never raises."""
        try:
            async with self.sessions() as session:
                await MemoryStore(session).add(**kwargs)  # type: ignore[arg-type]
                await session.commit()
        except ValidationFailed as exc:
            log.info("memory.rejected", reason=exc.message)
        except Exception as exc:
            log.warning("memory.write_failed", error=str(exc))
