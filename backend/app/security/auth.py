"""Authentication service: registration, login, refresh-token rotation, logout."""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.exceptions import (
    AuthenticationError,
    ConflictError,
    ErrorCode,
    PermissionDenied,
    ValidationFailed,
)
from app.database.base import utcnow
from app.database.models import Organization, OrgRole, RefreshToken, User
from app.database.repositories.identity import OrganizationRepository, RefreshTokenRepository, UserRepository
from app.security.passwords import hash_password, needs_rehash, validate_password_strength, verify_password
from app.security.tokens import create_access_token, hash_token, new_refresh_token

_EMAIL = re.compile(r"^[^@\s]{1,64}@[^@\s]+\.[^@\s]+$")
# constant dummy hash so login timing does not reveal whether an email exists
_DUMMY_HASH = hash_password("dummy-password-for-timing-9f8e7d")


@dataclass
class TokenPair:
    access_token: str
    access_expires_at: datetime
    refresh_token: str
    refresh_expires_at: datetime
    user: User
    organization_id: uuid.UUID


def _slugify(text: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return slug[:60] or "org"


class AuthService:
    def __init__(self, session: AsyncSession, settings: Settings) -> None:
        self.session = session
        self.settings = settings
        self.users = UserRepository(session)
        self.orgs = OrganizationRepository(session)
        self.refresh_tokens = RefreshTokenRepository(session)

    async def register(self, email: str, password: str, display_name: str = "") -> User:
        email = email.strip().lower()
        if not _EMAIL.match(email):
            raise ValidationFailed("Please enter a valid email address.")
        first_user = await self.users.count() == 0
        if not self.settings.allow_registration and not first_user:
            raise PermissionDenied("Registration is disabled on this server. Ask an administrator for an account.")
        validate_password_strength(password)
        if await self.users.by_email(email):
            raise ConflictError("An account with this email already exists.")
        user = await self.users.add(User(
            email=email, password_hash=hash_password(password),
            display_name=display_name.strip() or email.split("@")[0],
            is_superadmin=first_user,  # the first account administers the installation
        ))
        base_slug = _slugify(display_name or email.split("@")[0])
        slug = base_slug
        while await self.orgs.slug_exists(slug):
            slug = f"{base_slug}-{uuid.uuid4().hex[:6]}"
        org = await self.orgs.add(Organization(
            name=f"{user.display_name}'s workspace", slug=slug, is_personal=True, plan_id=self.settings.default_plan,
        ))
        await self.orgs.add_member(org.id, user.id, OrgRole.OWNER)
        return user

    async def authenticate(self, email: str, password: str) -> User:
        user = await self.users.by_email(email)
        if user is None:
            verify_password(_DUMMY_HASH, password)
            raise AuthenticationError("Incorrect email or password.", code=ErrorCode.INVALID_CREDENTIALS)
        if not verify_password(user.password_hash, password):
            raise AuthenticationError("Incorrect email or password.", code=ErrorCode.INVALID_CREDENTIALS)
        if not user.is_active:
            raise AuthenticationError("This account is disabled.", code=ErrorCode.INVALID_CREDENTIALS)
        if needs_rehash(user.password_hash):
            user.password_hash = hash_password(password)
        user.last_login_at = utcnow()
        return user

    async def default_org(self, user: User) -> uuid.UUID:
        orgs = await self.orgs.orgs_for_user(user.id)
        if not orgs:
            raise PermissionDenied("Your account is not a member of any organization.")
        return orgs[0][0].id

    async def issue_tokens(self, user: User, org_id: uuid.UUID, *, family_id: uuid.UUID | None = None,
                           user_agent: str = "") -> TokenPair:
        if await self.orgs.membership(org_id, user.id) is None:
            raise PermissionDenied("You are not a member of this organization.")
        access, access_exp = create_access_token(
            self.settings.secret_key.get_secret_value(), user_id=user.id, org_id=org_id,
            ttl_minutes=self.settings.access_token_ttl_minutes,
        )
        refresh, refresh_hash = new_refresh_token()
        refresh_exp = datetime.now(UTC) + timedelta(days=self.settings.refresh_token_ttl_days)
        await self.refresh_tokens.add(RefreshToken(
            user_id=user.id, token_hash=refresh_hash, family_id=family_id or uuid.uuid4(),
            expires_at=refresh_exp, user_agent=user_agent[:400],
        ))
        return TokenPair(access, access_exp, refresh, refresh_exp, user, org_id)

    async def refresh(self, refresh_token: str, org_id: uuid.UUID | None = None, user_agent: str = "") -> TokenPair:
        stored = await self.refresh_tokens.by_hash(hash_token(refresh_token))
        if stored is None:
            raise AuthenticationError("Your session is no longer valid. Please sign in again.")
        if stored.revoked_at is not None:
            # reuse of a rotated token: assume theft, revoke the whole family
            await self.refresh_tokens.revoke_family(stored.family_id)
            raise AuthenticationError("Your session is no longer valid. Please sign in again.")
        if stored.expires_at < datetime.now(UTC):
            raise AuthenticationError("Your session has expired. Please sign in again.")
        user = await self.users.get(stored.user_id)
        if user is None or not user.is_active:
            raise AuthenticationError("Your session is no longer valid. Please sign in again.")
        await self.refresh_tokens.revoke(stored)
        return await self.issue_tokens(user, org_id or await self.default_org(user),
                                       family_id=stored.family_id, user_agent=user_agent)

    async def logout(self, refresh_token: str) -> None:
        stored = await self.refresh_tokens.by_hash(hash_token(refresh_token))
        if stored is not None:
            await self.refresh_tokens.revoke_family(stored.family_id)
