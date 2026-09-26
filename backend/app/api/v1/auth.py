from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Cookie, Header, Request, Response

from app.api.deps import ContainerDep, PrincipalDep, SessionDep
from app.core.exceptions import AuthenticationError, PermissionDenied
from app.database.repositories.identity import OrganizationRepository
from app.schemas.common import LoginRequest, OrgOut, RegisterRequest, TokenResponse, UserOut
from app.security.auth import AuthService, TokenPair

router = APIRouter(prefix="/auth", tags=["auth"])
REFRESH_COOKIE = "ownai_refresh"


def _set_cookie(response: Response, pair: TokenPair, secure: bool) -> None:
    response.set_cookie(REFRESH_COOKIE, pair.refresh_token, httponly=True, secure=secure, samesite="strict",
                        path="/api/v1/auth", expires=pair.refresh_expires_at)


async def _token_response(session: SessionDep, pair: TokenPair) -> TokenResponse:
    orgs = OrganizationRepository(session)
    org = await orgs.get(pair.organization_id)
    membership = await orgs.membership(pair.organization_id, pair.user.id)
    assert org is not None and membership is not None
    return TokenResponse(access_token=pair.access_token, expires_at=pair.access_expires_at,
                         user=UserOut.model_validate(pair.user), organization=OrgOut.model_validate(org),
                         role=membership.role.value)


def _require_ajax(x_requested_with: str | None) -> None:
    # CSRF protection for cookie-authenticated endpoints: browsers never add this header cross-site.
    if x_requested_with != "ownai":
        raise PermissionDenied("Missing X-Requested-With header.")


@router.post("/register", response_model=TokenResponse, status_code=201)
async def register(body: RegisterRequest, request: Request, response: Response, session: SessionDep,
                   container: ContainerDep) -> TokenResponse:
    service = AuthService(session, container.settings)
    user = await service.register(body.email, body.password, body.display_name)
    pair = await service.issue_tokens(user, await service.default_org(user),
                                      user_agent=request.headers.get("user-agent", ""))
    await container.audit.record("auth.register", actor_id=user.id, resource_type="user", resource_id=user.id,
                                 organization_id=pair.organization_id, session=session)
    _set_cookie(response, pair, container.settings.cookie_secure)
    return await _token_response(session, pair)


@router.post("/login", response_model=TokenResponse)
async def login(body: LoginRequest, request: Request, response: Response, session: SessionDep,
                container: ContainerDep) -> TokenResponse:
    service = AuthService(session, container.settings)
    try:
        user = await service.authenticate(body.email, body.password)
    except AuthenticationError:
        await container.audit.record("auth.login_failed", actor_type="anonymous", outcome="failure",
                                     details={"email": body.email[:320]})
        raise
    pair = await service.issue_tokens(user, await service.default_org(user),
                                      user_agent=request.headers.get("user-agent", ""))
    await container.audit.record("auth.login", actor_id=user.id, organization_id=pair.organization_id,
                                 resource_type="user", resource_id=user.id, session=session)
    _set_cookie(response, pair, container.settings.cookie_secure)
    return await _token_response(session, pair)


@router.post("/refresh", response_model=TokenResponse)
async def refresh(request: Request, response: Response, session: SessionDep, container: ContainerDep,
                  ownai_refresh: Annotated[str | None, Cookie()] = None,
                  x_requested_with: Annotated[str | None, Header()] = None,
                  org_id: uuid.UUID | None = None) -> TokenResponse:
    _require_ajax(x_requested_with)
    if not ownai_refresh:
        raise AuthenticationError("Please sign in to continue.")
    pair = await AuthService(session, container.settings).refresh(ownai_refresh, org_id,
                                                                   user_agent=request.headers.get("user-agent", ""))
    _set_cookie(response, pair, container.settings.cookie_secure)
    return await _token_response(session, pair)


@router.post("/logout", status_code=204)
async def logout(response: Response, session: SessionDep, container: ContainerDep,
                 ownai_refresh: Annotated[str | None, Cookie()] = None,
                 x_requested_with: Annotated[str | None, Header()] = None) -> Response:
    _require_ajax(x_requested_with)
    if ownai_refresh:
        await AuthService(session, container.settings).logout(ownai_refresh)
    response.delete_cookie(REFRESH_COOKIE, path="/api/v1/auth")
    response.status_code = 204
    return response


@router.get("/me")
async def me(principal: PrincipalDep) -> dict:
    return {"user": UserOut.model_validate(principal.user).model_dump(mode="json"),
            "organization": OrgOut.model_validate(principal.org).model_dump(mode="json"),
            "role": principal.role.value}
