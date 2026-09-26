"""Password hashing with Argon2id."""

from __future__ import annotations

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError

from app.core.exceptions import ValidationFailed

_hasher = PasswordHasher()

MIN_PASSWORD_LENGTH = 10


def validate_password_strength(password: str) -> None:
    if len(password) < MIN_PASSWORD_LENGTH:
        raise ValidationFailed(f"Password must be at least {MIN_PASSWORD_LENGTH} characters long.")
    if password.lower() == password or password.upper() == password:
        if not any(c.isdigit() for c in password) and not any(not c.isalnum() for c in password):
            raise ValidationFailed("Password must mix upper/lower case letters, digits or symbols.")


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(password_hash: str, password: str) -> bool:
    try:
        return _hasher.verify(password_hash, password)
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False


def needs_rehash(password_hash: str) -> bool:
    return _hasher.check_needs_rehash(password_hash)
