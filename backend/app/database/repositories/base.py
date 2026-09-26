"""Shared repository helpers enforcing organization isolation."""

from __future__ import annotations

import uuid
from typing import Any, TypeVar

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import NotFoundError

T = TypeVar("T")


async def get_scoped(session: AsyncSession, model: type[T], obj_id: uuid.UUID, org_id: uuid.UUID,
                     *, label: str | None = None) -> T:
    """Fetch a tenant-owned row, raising NotFound when it belongs to another organization.

    Returning NotFound (not Forbidden) avoids leaking the existence of other tenants' data.
    """
    stmt = select(model).where(model.id == obj_id, model.organization_id == org_id)  # type: ignore[attr-defined]
    obj = (await session.execute(stmt)).scalar_one_or_none()
    if obj is None:
        raise NotFoundError(f"{label or model.__name__} not found.")
    return obj


async def list_scoped(session: AsyncSession, model: type[T], org_id: uuid.UUID, *filters: Any,
                      order_by: Any = None, limit: int = 100, offset: int = 0) -> list[T]:
    stmt = select(model).where(model.organization_id == org_id, *filters)  # type: ignore[attr-defined]
    if order_by is not None:
        stmt = stmt.order_by(order_by)
    stmt = stmt.limit(limit).offset(offset)
    return list((await session.execute(stmt)).scalars().all())
