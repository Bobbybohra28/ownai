from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.base import utcnow
from app.database.models import Membership, Organization, OrgRole, RefreshToken, User


class UserRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get(self, user_id: uuid.UUID) -> User | None:
        return await self.session.get(User, user_id)

    async def by_email(self, email: str) -> User | None:
        stmt = select(User).where(func.lower(User.email) == email.strip().lower())
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def count(self) -> int:
        return int((await self.session.execute(select(func.count(User.id)))).scalar_one())

    async def add(self, user: User) -> User:
        self.session.add(user)
        await self.session.flush()
        return user

    async def list_in_org(self, org_id: uuid.UUID) -> list[tuple[User, Membership]]:
        stmt = (select(User, Membership).join(Membership, Membership.user_id == User.id)
                .where(Membership.organization_id == org_id).order_by(User.email))
        return [(u, m) for u, m in (await self.session.execute(stmt)).all()]


class OrganizationRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get(self, org_id: uuid.UUID) -> Organization | None:
        return await self.session.get(Organization, org_id)

    async def slug_exists(self, slug: str) -> bool:
        stmt = select(func.count(Organization.id)).where(Organization.slug == slug)
        return bool((await self.session.execute(stmt)).scalar_one())

    async def add(self, org: Organization) -> Organization:
        self.session.add(org)
        await self.session.flush()
        return org

    async def membership(self, org_id: uuid.UUID, user_id: uuid.UUID) -> Membership | None:
        stmt = select(Membership).where(Membership.organization_id == org_id, Membership.user_id == user_id)
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def add_member(self, org_id: uuid.UUID, user_id: uuid.UUID, role: OrgRole) -> Membership:
        membership = Membership(organization_id=org_id, user_id=user_id, role=role)
        self.session.add(membership)
        await self.session.flush()
        return membership

    async def orgs_for_user(self, user_id: uuid.UUID) -> list[tuple[Organization, Membership]]:
        stmt = (select(Organization, Membership).join(Membership, Membership.organization_id == Organization.id)
                .where(Membership.user_id == user_id).order_by(Organization.created_at))
        return [(o, m) for o, m in (await self.session.execute(stmt)).all()]


class RefreshTokenRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def add(self, token: RefreshToken) -> None:
        self.session.add(token)
        await self.session.flush()

    async def by_hash(self, token_hash: str) -> RefreshToken | None:
        stmt = select(RefreshToken).where(RefreshToken.token_hash == token_hash)
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def revoke_family(self, family_id: uuid.UUID) -> None:
        await self.session.execute(
            update(RefreshToken).where(RefreshToken.family_id == family_id, RefreshToken.revoked_at.is_(None))
            .values(revoked_at=utcnow())
        )

    async def revoke(self, token: RefreshToken, when: datetime | None = None) -> None:
        token.revoked_at = when or utcnow()
