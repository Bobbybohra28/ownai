"""Configuration loaded from environment variables."""

import os

SECRET_KEY = os.environ.get("TASKBOARD_SECRET", "dev-only-secret-change-me")
TOKEN_TTL_SECONDS = int(os.environ.get("TASKBOARD_TOKEN_TTL", "3600"))
PBKDF2_ITERATIONS = 120_000
