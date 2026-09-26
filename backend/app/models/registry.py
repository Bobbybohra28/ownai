"""Model registry: loads model definitions from YAML (or the single-model env vars).

Model names/endpoints are *never* hard-coded in application code — they come from
``models.yaml`` (with ``${ENV}`` / ``${ENV:-default}`` interpolation) or from
``OWNAI_MODEL_URL`` / ``OWNAI_MODEL_NAME`` for the quick single-model setup.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field, ValidationError

from app.core.config import Settings
from app.core.exceptions import ConfigurationError
from app.core.logging import get_logger
from app.models.providers.base import CHAT_ROLES, ModelCapabilities, ModelConfig, ModelRole

log = get_logger(__name__)

_VAR = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-(.*?))?\}")


class _Unresolved(str):
    """Marker for values referencing unset environment variables."""


def interpolate(value: Any, missing: set[str]) -> Any:
    if isinstance(value, dict):
        return {k: interpolate(v, missing) for k, v in value.items()}
    if isinstance(value, list):
        return [interpolate(v, missing) for v in value]
    if not isinstance(value, str):
        return value

    def repl(match: re.Match[str]) -> str:
        name, default = match.group(1), match.group(2)
        env = os.environ.get(name)
        if env not in (None, ""):
            return env
        if default is not None:
            return default
        missing.add(name)
        return ""

    result = _VAR.sub(repl, value)
    if result != value and result == "" and missing:
        return None
    # keep integers/booleans typed when a whole value was a variable (never coerce names)
    if _VAR.fullmatch(value):
        if re.fullmatch(r"-?\d+", result):
            return int(result)
        if result.lower() in {"true", "false"}:
            return result.lower() == "true"
    return result


class RoutingPolicy(BaseModel):
    # Optional explicit ordering of candidate model ids per role.
    role_preferences: dict[ModelRole, list[str]] = Field(default_factory=dict)
    # When no model has the requested chat role, any healthy chat model may be used.
    allow_cross_role_fallback: bool = True
    # Complex tasks prefer reasoning-capable models for these roles.
    prefer_reasoning_for_complex: bool = True
    critic_prefers_different_model: bool = True


class RegistryWarning(BaseModel):
    model_id: str
    message: str


class ModelRegistry:
    def __init__(self, models: list[ModelConfig], policy: RoutingPolicy | None = None,
                 warnings: list[RegistryWarning] | None = None) -> None:
        ids = [m.id for m in models]
        if len(ids) != len(set(ids)):
            raise ConfigurationError("Duplicate model ids in model configuration.")
        self._models: dict[str, ModelConfig] = {m.id: m for m in models}
        self.policy = policy or RoutingPolicy()
        self.warnings = warnings or []
        self._overrides: dict[str, dict[str, Any]] = {}

    # ---- loading ----------------------------------------------------------------------------
    @classmethod
    def from_settings(cls, settings: Settings) -> ModelRegistry:
        path = settings.models_config_path
        if path:
            return cls.from_yaml(Path(path), default_timeout=settings.model_request_timeout_s)
        return cls.from_env(settings)

    @classmethod
    def from_yaml(cls, path: Path, *, default_timeout: float = 180.0) -> ModelRegistry:
        if not path.exists():
            raise ConfigurationError(f"Model configuration file not found: {path}")
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        return cls.from_dict(raw, default_timeout=default_timeout)

    @classmethod
    def from_dict(cls, raw: dict[str, Any], *, default_timeout: float = 180.0) -> ModelRegistry:
        models: list[ModelConfig] = []
        warnings: list[RegistryWarning] = []
        for model_id, entry in (raw.get("models") or {}).items():
            missing: set[str] = set()
            resolved = interpolate(entry or {}, missing)
            if not isinstance(resolved, dict):
                raise ConfigurationError(f"Model '{model_id}' must be a mapping.")
            if not resolved.get("endpoint") or not resolved.get("model"):
                reason = (f"missing environment variables: {', '.join(sorted(missing))}" if missing
                          else "endpoint and model are required")
                warnings.append(RegistryWarning(model_id=model_id, message=f"Model disabled — {reason}."))
                log.warning("model_registry.model_skipped", model_id=model_id, reason=reason)
                continue
            resolved.setdefault("timeout_s", default_timeout)
            resolved["id"] = model_id
            caps = resolved.get("capabilities") or {}
            roles = [ModelRole(r) for r in resolved.get("roles") or []]
            if ModelRole.EMBEDDING in roles:
                caps.setdefault("supports_embeddings", True)
                caps.setdefault("supports_chat", False)
                caps.setdefault("supports_streaming", False)
            if ModelRole.RERANKER in roles:
                caps.setdefault("supports_rerank", True)
                caps.setdefault("supports_chat", False)
                caps.setdefault("supports_streaming", False)
            if ModelRole.VISION in roles:
                caps.setdefault("supports_vision", True)
            resolved["capabilities"] = caps
            try:
                models.append(ModelConfig.model_validate(resolved))
            except ValidationError as exc:
                raise ConfigurationError(f"Invalid configuration for model '{model_id}'.", detail=str(exc)) from exc
        policy = RoutingPolicy.model_validate(interpolate(raw.get("routing") or {}, set()))
        return cls(models, policy, warnings)

    @classmethod
    def from_env(cls, settings: Settings) -> ModelRegistry:
        models: list[ModelConfig] = []
        warnings: list[RegistryWarning] = []
        if settings.model_url and settings.model_name:
            models.append(ModelConfig(
                id="default",
                provider=settings.model_provider,
                endpoint=settings.model_url,
                api_key=settings.model_api_key,
                model=settings.model_name,
                roles=[ModelRole.FAST, ModelRole.CODING, ModelRole.REASONING],
                context_length=settings.model_context_length,
                capabilities=ModelCapabilities(supports_tools=settings.model_supports_tools),
                timeout_s=settings.model_request_timeout_s,
            ))
        else:
            warnings.append(RegistryWarning(
                model_id="default",
                message="No chat model configured. Set OWNAI_MODELS_CONFIG_PATH or OWNAI_MODEL_URL + OWNAI_MODEL_NAME.",
            ))
        if settings.embedding_url and settings.embedding_model:
            models.append(ModelConfig(
                id="embedding",
                provider=settings.embedding_provider,
                endpoint=settings.embedding_url,
                api_key=settings.embedding_api_key,
                model=settings.embedding_model,
                roles=[ModelRole.EMBEDDING],
                embedding_dimensions=settings.embedding_dimensions,
                capabilities=ModelCapabilities(supports_chat=False, supports_streaming=False, supports_embeddings=True),
                timeout_s=settings.model_request_timeout_s,
            ))
        return cls(models, RoutingPolicy(), warnings)

    # ---- queries ----------------------------------------------------------------------------
    def apply_overrides(self, overrides: dict[str, dict[str, Any]]) -> None:
        """Admin overrides persisted in the database: {model_id: {"enabled": bool, "priority": int}}."""
        self._overrides = {k: {kk: vv for kk, vv in v.items() if vv is not None} for k, v in overrides.items()}

    def _effective(self, config: ModelConfig) -> ModelConfig:
        override = self._overrides.get(config.id)
        return config.model_copy(update=override) if override else config

    def all(self) -> list[ModelConfig]:
        return [self._effective(m) for m in self._models.values()]

    def enabled(self) -> list[ModelConfig]:
        return [m for m in self.all() if m.enabled]

    def get(self, model_id: str) -> ModelConfig | None:
        config = self._models.get(model_id)
        return self._effective(config) if config else None

    def by_role(self, role: ModelRole) -> list[ModelConfig]:
        return [m for m in self.enabled() if role in m.roles]

    def chat_models(self) -> list[ModelConfig]:
        return [m for m in self.enabled() if m.capabilities.supports_chat and (set(m.roles) & CHAT_ROLES or not m.roles)]

    def has_role(self, role: ModelRole) -> bool:
        return bool(self.by_role(role))
