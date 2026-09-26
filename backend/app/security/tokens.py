"""JWT access tokens and opaque rotating refresh tokens."""

from __future__ import annotations

import hashlib
import secrets
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import jwt

from app.core.exceptions import AuthenticationError, ErrorCode

ALGORITHM = "HS256"
ISSUER = "ownai"


@dataclass(frozen=True, slots=True)
class AccessClaims:
    user_id: uuid.UUID
    org_id: uuid.UUID
    expires_at: datetime


def create_access_token(secret: str, *, user_id: uuid.UUID, org_id: uuid.UUID, ttl_minutes: int) -> tuple[str, datetime]:
    now = datetime.now(UTC)
    expires = now + timedelta(minutes=ttl_minutes)
    payload = {
        "sub": str(user_id),
        "org": str(org_id),
        "iat": int(now.timestamp()),
        "exp": int(expires.timestamp()),
        "iss": ISSUER,
        "typ": "access",
        "jti": uuid.uuid4().hex,
    }
    return jwt.encode(payload, secret, algorithm=ALGORITHM), expires


def decode_access_token(secret: str, token: str) -> AccessClaims:
    try:
        payload = jwt.decode(token, secret, algorithms=[ALGORITHM], issuer=ISSUER,
                             options={"require": ["exp", "sub", "org", "iss"]})
    except jwt.ExpiredSignatureError as exc:
        raise AuthenticationError("Your session has expired. Please sign in again.") from exc
    except jwt.PyJWTError as exc:
        raise AuthenticationError("Invalid authentication token.", code=ErrorCode.AUTHENTICATION_REQUIRED) from exc
    if payload.get("typ") != "access":
        raise AuthenticationError("Invalid authentication token.")
    try:
        return AccessClaims(user_id=uuid.UUID(payload["sub"]), org_id=uuid.UUID(payload["org"]),
                            expires_at=datetime.fromtimestamp(payload["exp"], UTC))
    except (KeyError, ValueError) as exc:
        raise AuthenticationError("Invalid authentication token.") from exc


def new_refresh_token() -> tuple[str, str]:
    """Return (token, sha256 hash). Only the hash is stored."""
    token = secrets.token_urlsafe(48)
    return token, hash_token(token)


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()
