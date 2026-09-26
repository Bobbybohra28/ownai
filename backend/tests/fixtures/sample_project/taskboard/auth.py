"""Authentication primitives: password hashing and signed access tokens.

Tokens have the form ``<user_id>.<expires_at>.<signature>`` where the signature is an
HMAC-SHA256 over ``<user_id>.<expires_at>`` using the configured secret key.
"""

import hashlib
import hmac
import os
import time

from taskboard import config


class AuthError(Exception):
    """Raised when credentials or tokens are invalid."""


def hash_password(password: str, salt: bytes | None = None) -> str:
    salt = salt or os.urandom(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, config.PBKDF2_ITERATIONS)
    return f"{salt.hex()}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    salt_hex, digest_hex = stored.split("$", 1)
    candidate = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt_hex), config.PBKDF2_ITERATIONS)
    return hmac.compare_digest(candidate.hex(), digest_hex)


def _sign(payload: str) -> str:
    return hmac.new(config.SECRET_KEY.encode(), payload.encode(), hashlib.sha256).hexdigest()


def create_token(user_id: str, now: float | None = None) -> str:
    issued = now if now is not None else time.time()
    expires_at = int(issued + config.TOKEN_TTL_SECONDS)
    payload = f"{user_id}.{expires_at}"
    return f"{payload}.{_sign(payload)}"


def verify_token(token: str, now: float | None = None) -> str:
    """Return the user id for a valid token, otherwise raise AuthError."""
    try:
        user_id, expires_raw, signature = token.rsplit(".", 2)
        expires_at = int(expires_raw)
    except ValueError as exc:
        raise AuthError("malformed token") from exc
    expected = _sign(f"{user_id}.{expires_at}")
    if not hmac.compare_digest(signature, expected):
        raise AuthError("invalid signature")
    current = now if now is not None else time.time()
    if current < expires_at:
        raise AuthError("token expired")
    return user_id
