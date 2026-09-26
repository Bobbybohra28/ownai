"""Append-only audit logging to PostgreSQL (and the structured log stream)."""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from structlog.contextvars import get_contextvars

from app.core.logging import get_logger
from app.database.base import utcnow
from app.database.models import AuditLog
from app.security.secrets import redact_mapping

log = get_logger("audit")


class AuditLogger:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = session_factory

    async def record(
        self,
        action: str,
        *,
        organization_id: uuid.UUID | None = None,
        actor_type: str = "user",
        actor_id: str | uuid.UUID | None = None,
        resource_type: str | None = None,
        resource_id: str | uuid.UUID | None = None,
        outcome: str = "success",
        details: dict[str, Any] | None = None,
        ip: str | None = None,
        session: AsyncSession | None = None,
    ) -> None:
        ctx = get_contextvars()
        entry = AuditLog(
            organization_id=organization_id,
            actor_type=actor_type,
            actor_id=str(actor_id) if actor_id else None,
            action=action,
            resource_type=resource_type,
            resource_id=str(resource_id) if resource_id else None,
            request_id=ctx.get("request_id"),
            ip=ip or ctx.get("client_ip"),
            outcome=outcome,
            details=redact_mapping(details or {}),  # type: ignore[arg-type]
            created_at=utcnow(),
        )
        log.info("audit", action=action, actor_type=actor_type, actor_id=entry.actor_id,
                 resource_type=resource_type, resource_id=entry.resource_id, outcome=outcome)
        if session is not None:
            session.add(entry)
            return
        async with self._sessions() as own:
            own.add(entry)
            await own.commit()
