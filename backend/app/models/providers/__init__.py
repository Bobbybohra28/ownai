"""Provider factory."""

from __future__ import annotations

import httpx

from app.models.providers.base import ModelConfig, ModelProvider
from app.models.providers.ollama import OllamaProvider
from app.models.providers.openai_compatible import OpenAICompatibleProvider
from app.models.providers.vllm import VLLMProvider

PROVIDERS: dict[str, type[ModelProvider]] = {
    "openai_compatible": OpenAICompatibleProvider,
    "vllm": VLLMProvider,
    "ollama": OllamaProvider,
}


def create_provider(config: ModelConfig, http_client: httpx.AsyncClient) -> ModelProvider:
    try:
        cls = PROVIDERS[config.provider]
    except KeyError as exc:  # pragma: no cover - guarded by config validation
        raise ValueError(f"Unknown model provider '{config.provider}'") from exc
    return cls(config, http_client)


__all__ = ["PROVIDERS", "create_provider"]
