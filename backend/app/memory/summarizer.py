"""Conversation summarisation: older turns are condensed into a rolling summary so long
conversations keep their context without exceeding model windows."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import ModelError
from app.core.logging import get_logger
from app.database.models import Conversation, Message
from app.models.providers.base import ChatMessage, ChatRequest, ModelRole
from app.models.router import ModelRequirements, ModelRouter
from app.security.secrets import mask_secrets

log = get_logger(__name__)

KEEP_RECENT = 10
SUMMARIZE_BATCH_MIN = 6


async def maybe_summarize(session: AsyncSession, conversation: Conversation, router: ModelRouter) -> bool:
    """Summarise messages older than the most recent window. Returns True if the summary changed."""
    stmt = select(Message).where(Message.conversation_id == conversation.id,
                                 Message.role.in_(["user", "assistant"])).order_by(Message.created_at)
    messages = list((await session.execute(stmt)).scalars().all())
    if len(messages) <= KEEP_RECENT:
        return False
    older = messages[:-KEEP_RECENT]
    if conversation.summary_upto_message_id:
        ids = [m.id for m in older]
        if conversation.summary_upto_message_id in ids:
            older = older[ids.index(conversation.summary_upto_message_id) + 1:]
    if len(older) < SUMMARIZE_BATCH_MIN:
        return False
    transcript = "\n".join(f"{m.role}: {mask_secrets(m.content)[:1500]}" for m in older)
    try:
        routed = await router.chat(
            ChatRequest(messages=[
                ChatMessage(role="system", content=(
                    "Summarise this developer conversation for future context. Keep decisions, requirements, file "
                    "names, errors and open questions. Max 200 words. Never include secrets.")),
                ChatMessage(role="user", content=(f"Existing summary:\n{conversation.summary}\n\n" if conversation.summary
                                                  else "") + f"New messages:\n{transcript}"),
            ], max_tokens=400, temperature=0.1),
            ModelRequirements(role=ModelRole.FAST, complexity="simple", agent_id="memory_summarizer"),
        )
    except ModelError as exc:
        log.info("memory.summarize_skipped", code=exc.code)
        return False
    conversation.summary = mask_secrets(routed.response.content.strip())[:4000]
    conversation.summary_upto_message_id = older[-1].id
    return True
