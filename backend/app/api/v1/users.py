from __future__ import annotations

from fastapi import APIRouter

from app.api.deps import ContainerDep, PrincipalDep, SessionDep
from app.core.exceptions import AuthenticationError, ValidationFailed
from app.database.models import User
from app.schemas.common import ChangePasswordRequest, UpdateUserRequest, UserOut
from app.security.passwords import hash_password, validate_password_strength, verify_password
from app.security.secrets import contains_secret

router = APIRouter(prefix="/users", tags=["users"])

ALLOWED_PREFERENCES = {"default_mode", "theme", "preferred_languages", "coding_style", "response_style"}


@router.get("/me", response_model=UserOut)
async def get_me(principal: PrincipalDep) -> User:
    return principal.user


@router.patch("/me", response_model=UserOut)
async def update_me(body: UpdateUserRequest, principal: PrincipalDep, session: SessionDep) -> User:
    user = await session.merge(principal.user)
    if body.display_name is not None:
        user.display_name = body.display_name.strip()[:200]
    if body.preferences is not None:
        unknown = set(body.preferences) - ALLOWED_PREFERENCES
        if unknown:
            raise ValidationFailed(f"Unknown preferences: {', '.join(sorted(unknown))}")
        if contains_secret(str(body.preferences)):
            raise ValidationFailed("Preferences must not contain credentials.")
        user.preferences = {**(user.preferences or {}), **body.preferences}
    await session.flush()
    return user


@router.post("/me/password", status_code=204)
async def change_password(body: ChangePasswordRequest, principal: PrincipalDep, session: SessionDep,
                          container: ContainerDep) -> None:
    user = await session.merge(principal.user)
    if not verify_password(user.password_hash, body.current_password):
        raise AuthenticationError("The current password is incorrect.")
    validate_password_strength(body.new_password)
    user.password_hash = hash_password(body.new_password)
    await container.audit.record("user.password_changed", organization_id=principal.org_id, actor_id=user.id,
                                 resource_type="user", resource_id=user.id, session=session)
