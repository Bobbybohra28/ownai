"""FastAPI dependencies: container, DB session, authenticated user/org/role, permission checks."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from typing import Annotated

from fastapi import Depends, Header, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.dependencies import Container
from app.core.exceptions import AuthenticationError, PermissionDenied
from app.core.logging import bind_contextvars
from app.database.models import Organization, OrgRole, Project, User
from app.database.repositories.base import get_scoped
from app.database.repositories.identity import OrganizationRepository, UserRepository
from app.security.rbac import Permission, has_permission
from app.security.tokens import decode_access_token


def get_container(request: Request) -> Container:
    return request.app.state.container


ContainerDep = Annotated[Container, Depends(get_container)]


async def get_session(container: ContainerDep) -> AsyncIterator[AsyncSession]:
    async with container.sessions() as session:
        try:
            yield session
            await session.commit()
        except BaseException:
            await session.rollback()
            raise


# scope="function": commit *before* the response is sent, so a client's next request sees the writes
# (with the default request scope, "register → immediately call the API" could race the commit).
SessionDep = Annotated[AsyncSession, Depends(get_session, scope="function")]


@dataclass
class Principal:
    user: User
    org: Organization
    role: OrgRole

    @property
    def user_id(self) -> uuid.UUID:
        return self.user.id

    @property
    def org_id(self) -> uuid.UUID:
        return self.org.id

    def can(self, permission: Permission) -> bool:
        return has_permission(self.role, permission)


async def current_principal(container: ContainerDep, session: SessionDep,
                            authorization: Annotated[str | None, Header()] = None) -> Principal:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise AuthenticationError("Please sign in to continue.")
    claims = decode_access_token(container.settings.secret_key.get_secret_value(), authorization[7:].strip())
    user = await UserRepository(session).get(claims.user_id)
    if user is None or not user.is_active:
        raise AuthenticationError("Your account is not active.")
    orgs = OrganizationRepository(session)
    membership = await orgs.membership(claims.org_id, user.id)
    org = await orgs.get(claims.org_id)
    if membership is None or org is None:
        raise AuthenticationError("You are no longer a member of this organization. Please sign in again.")
    bind_contextvars(user_id=str(user.id), org_id=str(org.id))
    return Principal(user=user, org=org, role=membership.role)


PrincipalDep = Annotated[Principal, Depends(current_principal)]


def requires(permission: Permission) -> Callable[[Principal], Principal]:
    def check(principal: PrincipalDep) -> Principal:
        if not principal.can(permission):
            raise PermissionDenied(f"Your role ({principal.role.value}) does not allow this action.")
        return principal

    return check


def superadmin(principal: PrincipalDep) -> Principal:
    if not principal.user.is_superadmin:
        raise PermissionDenied("Only the installation administrator can do this.")
    return principal


async def project_for(project_id: uuid.UUID, principal: Principal, session: AsyncSession) -> Project:
    project = await get_scoped(session, Project, project_id, principal.org_id, label="Project")
    bind_contextvars(project_id=str(project.id))
    return project
