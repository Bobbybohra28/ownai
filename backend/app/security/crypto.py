"""Symmetric encryption for stored credentials (e.g. customer database passwords).

Uses Fernet (AES-128-CBC + HMAC-SHA256) with a key from ``OWNAI_ENCRYPTION_KEY``.
Multiple comma-separated keys are supported for rotation (first key encrypts).
"""

from __future__ import annotations

from cryptography.fernet import Fernet, InvalidToken, MultiFernet

from app.core.exceptions import ConfigurationError


class SecretBox:
    def __init__(self, keys: str) -> None:
        parts = [k.strip() for k in keys.split(",") if k.strip()]
        if not parts:
            raise ConfigurationError("OWNAI_ENCRYPTION_KEY is not configured; cannot store credentials.")
        try:
            self._fernet = MultiFernet([Fernet(k.encode()) for k in parts])
        except (ValueError, TypeError) as exc:
            raise ConfigurationError("OWNAI_ENCRYPTION_KEY is not a valid Fernet key.") from exc

    def encrypt(self, plaintext: str) -> bytes:
        return self._fernet.encrypt(plaintext.encode("utf-8"))

    def decrypt(self, ciphertext: bytes) -> str:
        try:
            return self._fernet.decrypt(ciphertext).decode("utf-8")
        except InvalidToken as exc:
            raise ConfigurationError("Stored credential could not be decrypted (wrong or rotated key).") from exc
