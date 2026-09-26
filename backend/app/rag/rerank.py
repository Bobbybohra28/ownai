"""Optional cross-encoder reranking via the RERANKER model role."""

from __future__ import annotations

from app.core.exceptions import ModelError
from app.core.logging import get_logger
from app.models.providers.base import ModelRole
from app.models.router import ModelRouter

log = get_logger(__name__)


class Reranker:
    def __init__(self, router: ModelRouter) -> None:
        self.router = router

    def configured(self) -> bool:
        return self.router.registry.has_role(ModelRole.RERANKER)

    async def rerank(self, query: str, documents: list[str], top_n: int) -> list[tuple[int, float]] | None:
        """Return [(index, score)] best-first, or None when reranking is unavailable (caller keeps fused order)."""
        if not self.configured() or not documents:
            return None
        try:
            results = await self.router.rerank(query, documents, top_n=min(top_n, len(documents)))
        except ModelError as exc:
            log.warning("rerank.unavailable", code=exc.code, error=exc.message)
            return None
        return [(r.index, r.score) for r in sorted(results, key=lambda r: -r.score) if 0 <= r.index < len(documents)]
