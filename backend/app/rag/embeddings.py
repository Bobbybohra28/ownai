"""Embedding service on top of the model router (EMBEDDING role)."""

from __future__ import annotations

import re

from app.core.exceptions import ErrorCode, ModelError
from app.models.providers.base import ModelConfig
from app.models.router import ModelRouter

BATCH_SIZE = 32
MAX_EMBED_CHARS = 6000


def collection_name(model: ModelConfig, dimensions: int) -> str:
    slug = re.sub(r"[^a-z0-9]+", "_", f"{model.id}_{model.model}".lower()).strip("_")[:60]
    return f"ownai_chunks__{slug}__{dimensions}"


class EmbeddingService:
    def __init__(self, router: ModelRouter) -> None:
        self.router = router

    def model(self) -> ModelConfig | None:
        return self.router.embedding_model()

    async def available(self) -> bool:
        model = self.model()
        if model is None:
            return False
        healthy, _ = await self.router.is_healthy(model.id)
        return healthy

    async def embed_documents(self, texts: list[str]) -> tuple[list[list[float]], ModelConfig]:
        model = self.model()
        if model is None:
            raise ModelError("No embedding model is configured.", code=ErrorCode.MODEL_UNAVAILABLE)
        prefix = model.embedding_document_prefix
        vectors: list[list[float]] = []
        for i in range(0, len(texts), BATCH_SIZE):
            batch = [prefix + t[:MAX_EMBED_CHARS] for t in texts[i:i + BATCH_SIZE]]
            result = await self.router.embed(batch)
            vectors.extend(result.vectors)
        return vectors, model

    async def embed_query(self, text: str) -> tuple[list[float], ModelConfig]:
        model = self.model()
        if model is None:
            raise ModelError("No embedding model is configured.", code=ErrorCode.MODEL_UNAVAILABLE)
        result = await self.router.embed([model.embedding_query_prefix + text[:MAX_EMBED_CHARS]])
        return result.vectors[0], model
