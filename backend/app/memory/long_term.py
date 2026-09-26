"""Long-term memory store (user / project / conversation / task / agent scopes).

Secrets are never stored: every write is checked with the secret detector and rejected
if it contains a credential.
"""

from __future__ import annotations

import re
import uuid
from typing import Any

from sqlalchemy import Select, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import NotFoundError, ValidationFailed
from app.database.models import Memory
from app.security.secrets import contains_secret

SCOPES = {"user", "project", "conversation", "task", "agent"}
KINDS = {"preference", "style", "decision", "fact", "fix", "summary", "note", "architecture", "dependency", "lesson"}
_SENSITIVE_WORDS = re.compile(r"(?i)\b(password|passwd|api[_ ]?key|secret key|private key|access token)\s*(is|=|:)\s*\S+")


def check_storable(content: str) -> None:
    if not content or not content.strip():
        raise ValidationFailed("Memory content cannot be empty.")
    if len(content) > 4000:
        raise ValidationFailed("Memory content is too long (max 4000 characters).")
    if contains_secret(content) or _SENSITIVE_WORDS.search(content):
        raise ValidationFailed("This looks like it contains a credential. Secrets are never stored in memory.")


class MemoryStore:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def add(self, *, org_id: uuid.UUID, scope: str, kind: str, content: str, user_id: uuid.UUID | None = None,
                  project_id: uuid.UUID | None = None, conversation_id: uuid.UUID | None = None,
                  task_id: uuid.UUID | None = None, agent_id: str | None = None, source: str = "user",
                  confirmed: bool = True, importance: int = 1) -> Memory:
        if scope not in SCOPES:
            raise ValidationFailed(f"Unknown memory scope '{scope}'.")
        if kind not in KINDS:
            raise ValidationFailed(f"Unknown memory kind '{kind}'.")
        check_storable(content)
        memory = Memory(organization_id=org_id, scope=scope, kind=kind, content=content.strip(), user_id=user_id,
                        project_id=project_id, conversation_id=conversation_id, task_id=task_id, agent_id=agent_id,
                        source=source, confirmed=confirmed, importance=importance)
        self.session.add(memory)
        await self.session.flush()
        return memory

    async def get(self, memory_id: uuid.UUID, org_id: uuid.UUID) -> Memory:
        memory = (await self.session.execute(select(Memory).where(Memory.id == memory_id,
                                                                  Memory.organization_id == org_id))).scalar_one_or_none()
        if memory is None:
            raise NotFoundError("Memory not found.")
        return memory

    def _scoped(self, org_id: uuid.UUID, *, user_id: uuid.UUID | None, project_id: uuid.UUID | None,
                agent_id: str | None = None) -> Select[Any]:
        conditions = []
        if user_id:
            conditions.append((Memory.scope == "user") & (Memory.user_id == user_id))
        if project_id:
            conditions.append((Memory.scope == "project") & (Memory.project_id == project_id))
        if agent_id:
            conditions.append((Memory.scope == "agent") & (Memory.agent_id == agent_id) &
                              ((Memory.project_id == project_id) if project_id else Memory.project_id.is_(None)))
        stmt = select(Memory).where(Memory.organization_id == org_id, Memory.confirmed.is_(True))
        return stmt.where(or_(*conditions)) if conditions else stmt.where(Memory.id.is_(None))

    async def list(self, org_id: uuid.UUID, *, scope: str | None = None, user_id: uuid.UUID | None = None,
                   project_id: uuid.UUID | None = None, limit: int = 200) -> list[Memory]:
        stmt = select(Memory).where(Memory.organization_id == org_id)
        if scope:
            stmt = stmt.where(Memory.scope == scope)
        if user_id:
            stmt = stmt.where(or_(Memory.user_id == user_id, Memory.scope != "user"))
        if project_id:
            stmt = stmt.where(Memory.project_id == project_id)
        return list((await self.session.execute(stmt.order_by(Memory.created_at.desc()).limit(limit))).scalars().all())

    async def relevant(self, org_id: uuid.UUID, query: str, *, user_id: uuid.UUID | None, project_id: uuid.UUID | None,
                       agent_id: str | None = None, limit: int = 8) -> list[Memory]:
        """Preferences always apply; other memories are ranked by full-text relevance to the query."""
        base = self._scoped(org_id, user_id=user_id, project_id=project_id, agent_id=agent_id)
        always = list((await self.session.execute(
            base.where(Memory.kind.in_(["preference", "style"])).order_by(Memory.importance.desc()).limit(6)
        )).scalars().all())
        terms = [t for t in re.findall(r"[a-zA-Z_]{3,}", query.lower())][:12]
        ranked: list[Memory] = []
        if terms:
            tsq = func.to_tsquery("simple", " | ".join(f"{t}:*" for t in terms))
            stmt = (base.where(Memory.kind.notin_(["preference", "style"]), Memory.search_vector.op("@@")(tsq))
                    .order_by((func.ts_rank(Memory.search_vector, tsq) * Memory.importance).desc()).limit(limit))
            ranked = list((await self.session.execute(stmt)).scalars().all())
        seen: set[uuid.UUID] = set()
        return [m for m in always + ranked if not (m.id in seen or seen.add(m.id))][:limit + 6]  # type: ignore[func-returns-value]

    async def delete(self, memory: Memory) -> None:
        await self.session.delete(memory)
