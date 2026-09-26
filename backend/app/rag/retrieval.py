"""Hybrid retrieval: dense (Qdrant) + lexical (PostgreSQL full-text) + symbol matching,
fused with Reciprocal Rank Fusion and optionally reranked.

If embeddings or Qdrant are unavailable the retriever degrades to lexical search and
says so in ``notices`` — it never pretends semantic search ran.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import Integer, Select, cast, func, literal, or_, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.exceptions import ModelError, RAGError
from app.core.logging import get_logger
from app.database.models import DocumentChunk
from app.rag.chunking import split_identifier
from app.rag.embeddings import EmbeddingService, collection_name
from app.rag.rerank import Reranker
from app.rag.vector_store import VectorStore

log = get_logger(__name__)

RRF_K = 60
_STOPWORDS = {
    "the", "a", "an", "and", "or", "of", "to", "in", "on", "for", "with", "is", "are", "was", "were", "be", "been",
    "how", "what", "where", "when", "why", "which", "who", "does", "do", "did", "can", "could", "should", "would",
    "my", "our", "your", "this", "that", "these", "those", "it", "its", "me", "i", "we", "you", "about", "from",
    "explain", "tell", "show", "find", "work", "works", "working", "project", "code", "file", "files", "please",
    "there", "their", "into", "any", "all", "some", "use", "used", "using", "have", "has", "had", "get", "make",
}
# simple normalisation so "authentication" also finds "auth", "authenticate", ...
_EXPANSIONS = {
    "authentication": ["auth", "authenticate", "login", "token", "password", "jwt", "session"],
    "authorization": ["auth", "permission", "role", "rbac", "access"],
    "login": ["auth", "signin", "authenticate", "password"],
    "database": ["db", "sql", "engine", "session", "connection"],
    "config": ["settings", "configuration", "env"],
    "configuration": ["config", "settings", "env"],
    "error": ["exception", "raise", "fail", "error"],
    "bug": ["error", "exception", "fix"],
    "test": ["tests", "pytest", "assert"],
    "endpoint": ["route", "router", "api", "get", "post"],
    "api": ["route", "router", "endpoint"],
}


@dataclass
class RetrievedChunk:
    chunk_id: uuid.UUID
    file_path: str
    start_line: int
    end_line: int
    content: str
    symbol: str | None
    language: str | None
    document_type: str
    document_id: uuid.UUID | None
    score: float = 0.0
    sources: list[str] = field(default_factory=list)  # which retrieval arms found it

    def to_dict(self) -> dict[str, Any]:
        return {
            "chunk_id": str(self.chunk_id), "file_path": self.file_path, "start_line": self.start_line,
            "end_line": self.end_line, "symbol": self.symbol, "language": self.language,
            "document_type": self.document_type, "score": round(self.score, 4), "sources": self.sources,
        }


@dataclass
class RetrievalResult:
    chunks: list[RetrievedChunk]
    used_dense: bool
    used_lexical: bool
    used_rerank: bool
    notices: list[str] = field(default_factory=list)


def query_terms(query: str) -> list[str]:
    raw = re.findall(r"[A-Za-z_][A-Za-z0-9_]*", query)
    terms: list[str] = []
    for token in raw:
        lowered = token.lower()
        if lowered in _STOPWORDS or len(lowered) < 2:
            continue
        terms.append(lowered)
        if "_" in token or any(c.isupper() for c in token[1:]):
            terms.extend(split_identifier(token))
        terms.extend(_EXPANSIONS.get(lowered, []))
        if lowered.endswith("s") and len(lowered) > 4:
            terms.append(lowered[:-1])
    seen: set[str] = set()
    return [t for t in terms if not (t in seen or seen.add(t))][:24]  # type: ignore[func-returns-value]


def build_tsquery(terms: list[str]) -> str:
    safe = [re.sub(r"[^a-z0-9_]", "", t) for t in terms]
    return " | ".join(f"{t}:*" for t in safe if t)


class HybridRetriever:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession], vector_store: VectorStore,
                 embeddings: EmbeddingService, reranker: Reranker) -> None:
        self.sessions = session_factory
        self.vector_store = vector_store
        self.embeddings = embeddings
        self.reranker = reranker

    def _base_filters(self, stmt: Select[Any], org_id: uuid.UUID, project_id: uuid.UUID | None,
                      filters: dict[str, Any]) -> Select[Any]:
        stmt = stmt.where(DocumentChunk.organization_id == org_id)
        if project_id is not None:
            stmt = stmt.where(DocumentChunk.project_id == project_id)
        for key in ("language", "document_type", "file_path", "document_id"):
            value = filters.get(key)
            if value is None:
                continue
            column = getattr(DocumentChunk, key)
            stmt = stmt.where(column.in_(value) if isinstance(value, list | tuple | set) else column == value)
        if filters.get("path_prefix"):
            stmt = stmt.where(DocumentChunk.file_path.startswith(str(filters["path_prefix"])))
        return stmt

    async def _lexical(self, session: AsyncSession, query: str, org_id: uuid.UUID, project_id: uuid.UUID | None,
                       filters: dict[str, Any], limit: int) -> list[tuple[DocumentChunk, float]]:
        terms = query_terms(query)
        tsquery = build_tsquery(terms)
        if not tsquery:
            return []
        q = func.to_tsquery("simple", tsquery)
        rank = func.ts_rank_cd(DocumentChunk.search_vector, q, 32).label("rank")
        stmt = select(DocumentChunk, rank).where(DocumentChunk.search_vector.op("@@")(q))
        stmt = self._base_filters(stmt, org_id, project_id, filters)
        # symbol / path matches are strong signals for code questions
        boost_terms = [t for t in terms if len(t) > 2][:8]
        if boost_terms:
            boost = func.greatest(*[
                cast(or_(DocumentChunk.symbol.ilike(f"%{t}%"), DocumentChunk.file_path.ilike(f"%{t}%")), Integer)
                for t in boost_terms
            ], literal(0))
            stmt = stmt.order_by((rank + boost * 0.5).desc())
        else:
            stmt = stmt.order_by(rank.desc())
        rows = (await session.execute(stmt.limit(limit))).all()
        return [(row[0], float(row[1])) for row in rows]

    async def _dense(self, session: AsyncSession, query: str, org_id: uuid.UUID, project_id: uuid.UUID | None,
                     filters: dict[str, Any], limit: int) -> list[tuple[DocumentChunk, float]]:
        vector, model = await self.embeddings.embed_query(query)
        name = collection_name(model, len(vector))
        qfilters: dict[str, Any] = {k: filters.get(k) for k in ("language", "document_type", "file_path", "document_id")}
        if project_id is not None:
            qfilters["project_id"] = str(project_id)
        hits = await self.vector_store.search(name, vector, org_id=org_id, filters=qfilters, limit=limit)
        if not hits:
            return []
        ids = [h.id for h in hits]
        stmt = self._base_filters(select(DocumentChunk).where(DocumentChunk.id.in_(ids)), org_id, project_id, filters)
        rows = {c.id: c for c in (await session.execute(stmt)).scalars().all()}
        return [(rows[h.id], h.score) for h in hits if h.id in rows]  # DB check enforces tenancy again

    async def retrieve(self, query: str, *, org_id: uuid.UUID, project_id: uuid.UUID | None,
                       filters: dict[str, Any] | None = None, top_k: int = 8, candidates: int = 30) -> RetrievalResult:
        filters = filters or {}
        notices: list[str] = []
        fused: dict[uuid.UUID, RetrievedChunk] = {}
        used_dense = used_lexical = False

        def add(results: list[tuple[DocumentChunk, float]], arm: str) -> None:
            for rank, (chunk, _score) in enumerate(results):
                item = fused.get(chunk.id)
                if item is None:
                    item = RetrievedChunk(chunk.id, chunk.file_path, chunk.start_line, chunk.end_line, chunk.content,
                                          chunk.symbol, chunk.language, chunk.document_type, chunk.document_id)
                    fused[chunk.id] = item
                item.score += 1.0 / (RRF_K + rank + 1)
                item.sources.append(arm)

        async with self.sessions() as session:
            try:
                add(await self._lexical(session, query, org_id, project_id, filters, candidates), "lexical")
                used_lexical = True
            except Exception as exc:
                log.error("retrieval.lexical_failed", error=str(exc))
                notices.append("Keyword search failed; results may be incomplete.")
            if self.embeddings.model() is None:
                notices.append("No embedding model is configured; using keyword search only.")
            else:
                try:
                    add(await self._dense(session, query, org_id, project_id, filters, candidates), "semantic")
                    used_dense = True
                except (ModelError, RAGError) as exc:
                    log.warning("retrieval.dense_unavailable", code=getattr(exc, "code", None), error=str(exc))
                    notices.append(f"Semantic search unavailable ({exc.message}); using keyword search only.")

        wants_tests = bool(re.search(r"\btests?\b|pytest|spec", query, re.IGNORECASE))
        if not wants_tests:
            for item in fused.values():
                if "/test" in f"/{item.file_path}" or item.file_path.rsplit("/", 1)[-1].startswith("test_"):
                    item.score *= 0.8  # prefer implementation code unless the question is about tests
        ranked = sorted(fused.values(), key=lambda c: -c.score)
        used_rerank = False
        if ranked and self.reranker.configured():
            pool = ranked[:candidates]
            order = await self.reranker.rerank(query, [c.content[:2000] for c in pool], top_n=top_k)
            if order is not None:
                chosen = {i for i, _ in order}
                ranked = [pool[i] for i, _ in order] + [c for i, c in enumerate(pool) if i not in chosen]
                used_rerank = True
            else:
                notices.append("Reranker unavailable; using fused ranking.")
        if not ranked and not notices:
            notices.append("No indexed content matched this query.")
        return RetrievalResult(ranked[:top_k], used_dense, used_lexical, used_rerank, notices)
