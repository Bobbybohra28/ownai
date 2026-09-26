"""Application settings.

Precedence (highest first): environment variables / .env file > YAML file named by
``OWNAI_CONFIG_FILE`` > defaults below. Secrets are only ever read from the
environment (or a secret manager that populates it) and are typed ``SecretStr`` so
they never appear in reprs or logs.
"""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Annotated, Any, Literal

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import (
    BaseSettings,
    NoDecode,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
    YamlConfigSettingsSource,
)

REPO_ROOT = Path(__file__).resolve().parents[3]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="OWNAI_",
        env_file=(".env", str(REPO_ROOT / ".env")),
        env_file_encoding="utf-8",
        extra="ignore",
        env_nested_delimiter="__",
    )

    # --- runtime ---------------------------------------------------------------------------
    app_env: Literal["development", "test", "production"] = "development"
    log_level: str = "INFO"
    log_json: bool = True
    api_prefix: str = "/api/v1"
    cors_origins: Annotated[list[str], NoDecode] = Field(default_factory=lambda: ["http://localhost:5173"])
    # "worker": runs are executed by a separate `worker` process via Redis Streams.
    # "inline": runs execute inside the API process (single-process dev / tests).
    execution_mode: Literal["worker", "inline"] = "worker"

    # --- storage ---------------------------------------------------------------------------
    database_url: str = "postgresql+asyncpg://ownai:ownai@localhost:5432/ownai"
    database_pool_size: int = 10
    redis_url: str = "redis://localhost:6379/0"
    qdrant_url: str = "http://localhost:6333"
    qdrant_api_key: SecretStr | None = None
    projects_root: Path = REPO_ROOT / "data" / "projects"
    # Directories from which "local path" imports are permitted (empty = disabled).
    local_import_roots: Annotated[list[Path], NoDecode] = Field(default_factory=list)
    max_upload_mb: int = 200
    max_project_files: int = 20_000
    max_indexed_file_kb: int = 512

    # --- security --------------------------------------------------------------------------
    secret_key: SecretStr = SecretStr("")
    encryption_key: SecretStr = SecretStr("")
    access_token_ttl_minutes: int = 30
    refresh_token_ttl_days: int = 14
    allow_registration: bool = True
    cookie_secure: bool = False

    # --- configuration files ----------------------------------------------------------------
    models_config_path: Path | None = None
    agents_config_dir: Path = REPO_ROOT / "config" / "agents"
    plans_config_path: Path | None = None

    # --- single-model quick configuration (used when no models.yaml is given) ----------------
    model_provider: Literal["openai_compatible", "vllm", "ollama"] = "openai_compatible"
    model_url: str | None = None
    model_name: str | None = None
    model_api_key: SecretStr | None = None
    model_context_length: int = 8192
    model_supports_tools: bool = False
    embedding_provider: Literal["openai_compatible", "vllm", "ollama"] = "openai_compatible"
    embedding_url: str | None = None
    embedding_model: str | None = None
    embedding_api_key: SecretStr | None = None
    embedding_dimensions: int | None = None

    # --- model runtime ---------------------------------------------------------------------
    model_health_interval_s: int = 30
    model_health_ttl_s: int = 300
    model_request_timeout_s: float = 180.0

    # --- sandbox ---------------------------------------------------------------------------
    sandbox_url: str | None = "http://localhost:8090"
    sandbox_token: SecretStr = SecretStr("")
    sandbox_default_timeout_s: int = 120
    sandbox_max_upload_mb: int = 200

    # --- orchestration limits ----------------------------------------------------------------
    max_plan_steps: int = 12
    max_run_seconds: int = 1800
    max_fix_iterations: int = 2

    # --- billing ---------------------------------------------------------------------------
    billing_enabled: bool = False
    default_plan: str = "private"

    @field_validator("cors_origins", "local_import_roots", mode="before")
    @classmethod
    def _split_csv(cls, value: Any) -> Any:
        if isinstance(value, str):
            return [v.strip() for v in value.split(",") if v.strip()]
        return value

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        sources: list[PydanticBaseSettingsSource] = [init_settings, env_settings, dotenv_settings]
        config_file = os.environ.get("OWNAI_CONFIG_FILE")
        if config_file:
            sources.append(YamlConfigSettingsSource(settings_cls, yaml_file=config_file))
        sources.append(file_secret_settings)
        return tuple(sources)

    # --- derived ---------------------------------------------------------------------------
    @property
    def is_production(self) -> bool:
        return self.app_env == "production"

    def validate_for_runtime(self) -> list[str]:
        """Return a list of configuration problems that must block startup."""
        problems: list[str] = []
        if len(self.secret_key.get_secret_value()) < 32:
            problems.append("OWNAI_SECRET_KEY must be set to a random value of at least 32 characters.")
        if not self.encryption_key.get_secret_value():
            problems.append(
                "OWNAI_ENCRYPTION_KEY must be set (generate with: "
                "python -c \"from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())\")."
            )
        if self.is_production and not self.sandbox_token.get_secret_value():
            problems.append("OWNAI_SANDBOX_TOKEN must be set in production.")
        return problems


@lru_cache
def get_settings() -> Settings:
    return Settings()
