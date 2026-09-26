from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Request, Response

from app.api.deps import ContainerDep, Principal, PrincipalDep, SessionDep, requires
from app.api.v1.auth import _set_cookie, _token_response
from app.core.exceptions import ConflictError, NotFoundError, PermissionDenied
from app.database.models import OrgRole
from app.database.repositories.identity import OrganizationRepository, UserRepository
from app.schemas.common import AddMemberRequest, OrgOut, TokenResponse, UpdateMemberRequest
from app.security.auth import AuthService
from app.security.rbac import Permission

router = APIRouter(prefix="/orgs", tags=["organizations"])


@router.get("")
async def my_orgs(principal: PrincipalDep, session: SessionDep) -> list[dict]:
    rows = await OrganizationRepository(session).orgs_for_user(principal.user_id)
    return [{**OrgOut.model_validate(o).model_dump(mode="json"), "role": m.role.value,
             "current": o.id == principal.org_id} for o, m in rows]


@router.get("/current")
async def current_org(principal: PrincipalDep, container: ContainerDep) -> dict:
    plan = container.billing.plan_for(principal.org)
    return {**OrgOut.model_validate(principal.org).model_dump(mode="json"), "role": principal.role.value,
            "plan": plan.model_dump()}


@router.post("/{org_id}/switch", response_model=TokenResponse)
async def switch_org(org_id: uuid.UUID, request: Request, response: Response, principal: PrincipalDep,
                     session: SessionDep, container: ContainerDep) -> TokenResponse:
    pair = await AuthService(session, container.settings).issue_tokens(
        principal.user, org_id, user_agent=request.headers.get("user-agent", ""))
    _set_cookie(response, pair, container.settings.cookie_secure)
    return await _token_response(session, pair)


@router.get("/current/members")
async def members(principal: PrincipalDep, session: SessionDep) -> list[dict]:
    rows = await UserRepository(session).list_in_org(principal.org_id)
    return [{"user_id": str(u.id), "email": u.email, "display_name": u.display_name, "role": m.role.value}
            for u, m in rows]


@router.post("/current/members", status_code=201)
async def add_member(body: AddMemberRequest, session: SessionDep, container: ContainerDep,
                     principal: Principal = Depends(requires(Permission.MEMBERS_MANAGE))) -> dict:
    user = await UserRepository(session).by_email(body.email)
    if user is None:
        raise NotFoundError("No account exists with that email. Ask the person to register first.")
    orgs = OrganizationRepository(session)
    if await orgs.membership(principal.org_id, user.id):
        raise ConflictError("That user is already a member.")
    await container.billing.check(session, principal.org, "members")
    await orgs.add_member(principal.org_id, user.id, OrgRole(body.role))
    await container.audit.record("org.member_added", organization_id=principal.org_id, actor_id=principal.user_id,
                                 resource_type="user", resource_id=user.id, details={"role": body.role}, session=session)
    return {"user_id": str(user.id), "email": user.email, "role": body.role}


@router.patch("/current/members/{user_id}")
async def update_member(user_id: uuid.UUID, body: UpdateMemberRequest, session: SessionDep, container: ContainerDep,
                        principal: Principal = Depends(requires(Permission.MEMBERS_MANAGE))) -> dict:
    membership = await OrganizationRepository(session).membership(principal.org_id, user_id)
    if membership is None:
        raise NotFoundError("Member not found.")
    if membership.role == OrgRole.OWNER:
        raise PermissionDenied("The owner's role cannot be changed.")
    membership.role = OrgRole(body.role)
    await container.audit.record("org.member_role_changed", organization_id=principal.org_id, actor_id=principal.user_id,
                                 resource_type="user", resource_id=user_id, details={"role": body.role}, session=session)
    return {"user_id": str(user_id), "role": body.role}


@router.delete("/current/members/{user_id}", status_code=204)
async def remove_member(user_id: uuid.UUID, session: SessionDep, container: ContainerDep,
                        principal: Principal = Depends(requires(Permission.MEMBERS_MANAGE))) -> None:
    membership = await OrganizationRepository(session).membership(principal.org_id, user_id)
    if membership is None:
        raise NotFoundError("Member not found.")
    if membership.role == OrgRole.OWNER:
        raise PermissionDenied("The owner cannot be removed.")
    await session.delete(membership)
    await container.audit.record("org.member_removed", organization_id=principal.org_id, actor_id=principal.user_id,
                                 resource_type="user", resource_id=user_id, session=session)
