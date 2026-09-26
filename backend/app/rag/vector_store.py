"""Vector store abstraction with a Qdrant implementation."""

from __future__ import annotations

import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any

from qdrant_client import AsyncQdrantClient
from qdrant_client.http import models as qm
from qdrant_client.http.exceptions import ResponseHandlingException, UnexpectedResponse

from app.core.exceptions import ErrorCode, RAGError
from app.core.logging import get_logger

log = get_logger(__name__)

INDEXED_PAYLOAD_FIELDS: dict[str, qm.PayloadSchemaType] = {
    "project_id": qm.PayloadSchemaType.KEYWORD,
    "file_path": qm.PayloadSchemaType.KEYWORD,
    "language": qm.PayloadSchemaType.KEYWORD,
    "document_type": qm.PayloadSchemaType.KEYWORD,
    "symbol": qm.PayloadSchemaType.KEYWORD,
    "document_id": qm.PayloadSchemaType.KEYWORD,
}


@dataclass
class VectorPoint:
    id: uuid.UUID
    vector: list[float]
    payload: dict[str, Any]


@dataclass
class VectorHit:
    id: uuid.UUID
    score: float
    payload: dict[str, Any]


class VectorStore(ABC):
    @abstractmethod
    async def ensure_collection(self, name: str, dimensions: int) -> None: ...

    @abstractmethod
    async def upsert(self, name: str, points: list[VectorPoint]) -> None: ...

    @abstractmethod
    async def search(self, name: str, vector: list[float], *, org_id: uuid.UUID, filters: dict[str, Any],
                     limit: int) -> list[VectorHit]: ...

    @abstractmethod
    async def delete(self, name: str, *, org_id: uuid.UUID, filters: dict[str, Any]) -> None: ...

    @abstractmethod
    async def delete_ids(self, name: str, ids: list[uuid.UUID]) -> None: ...

    @abstractmethod
    async def health(self) -> bool: ...


def _unavailable(exc: Exception) -> RAGError:
    return RAGError("The vector database (Qdrant) is unavailable; semantic search is temporarily disabled.",
                    code=ErrorCode.VECTOR_STORE_UNAVAILABLE, detail=f"{type(exc).__name__}: {exc}")


class QdrantVectorStore(VectorStore):
    def __init__(self, url: str, api_key: str | None = None) -> None:
        self.client = AsyncQdrantClient(url=url, api_key=api_key or None, timeout=30)
        self._known: set[str] = set()

    async def ensure_collection(self, name: str, dimensions: int) -> None:
        if name in self._known:
            return
        try:
            if not await self.client.collection_exists(name):
                await self.client.create_collection(
                    collection_name=name,
                    vectors_config=qm.VectorParams(size=dimensions, distance=qm.Distance.COSINE),
                )
                await self.client.create_payload_index(
                    name, "org_id", field_schema=qm.KeywordIndexParams(type=qm.KeywordIndexType.KEYWORD, is_tenant=True)
                )
                for field, schema in INDEXED_PAYLOAD_FIELDS.items():
                    await self.client.create_payload_index(name, field, field_schema=schema)
                log.info("vector_store.collection_created", collection=name, dimensions=dimensions)
            else:
                info = await self.client.get_collection(name)
                params = info.config.params.vectors
                size = params.size if isinstance(params, qm.VectorParams) else None
                if size is not None and size != dimensions:
                    raise RAGError(f"Collection '{name}' has {size} dimensions but the embedding model produces {dimensions}.",
                                   code=ErrorCode.RAG_ERROR)
        except (ResponseHandlingException, UnexpectedResponse, OSError) as exc:
            raise _unavailable(exc) from exc
        self._known.add(name)

    async def upsert(self, name: str, points: list[VectorPoint]) -> None:
        if not points:
            return
        try:
            await self.client.upsert(
                collection_name=name,
                points=[qm.PointStruct(id=str(p.id), vector=p.vector, payload=p.payload) for p in points],
                wait=True,
            )
        except (ResponseHandlingException, UnexpectedResponse, OSError) as exc:
            raise _unavailable(exc) from exc

    @staticmethod
    def _filter(org_id: uuid.UUID, filters: dict[str, Any]) -> qm.Filter:
        must: list[qm.Condition] = [qm.FieldCondition(key="org_id", match=qm.MatchValue(value=str(org_id)))]
        for key, value in filters.items():
            if value is None:
                continue
            if isinstance(value, list | tuple | set):
                must.append(qm.FieldCondition(key=key, match=qm.MatchAny(any=[str(v) for v in value])))
            else:
                must.append(qm.FieldCondition(key=key, match=qm.MatchValue(value=str(value))))
        return qm.Filter(must=must)

    async def search(self, name: str, vector: list[float], *, org_id: uuid.UUID, filters: dict[str, Any],
                     limit: int) -> list[VectorHit]:
        try:
            if not await self.client.collection_exists(name):
                return []
            response = await self.client.query_points(
                collection_name=name, query=vector, query_filter=self._filter(org_id, filters), limit=limit,
                with_payload=True,
            )
        except (ResponseHandlingException, UnexpectedResponse, OSError) as exc:
            raise _unavailable(exc) from exc
        return [VectorHit(id=uuid.UUID(str(p.id)), score=float(p.score), payload=p.payload or {}) for p in response.points]

    async def delete(self, name: str, *, org_id: uuid.UUID, filters: dict[str, Any]) -> None:
        try:
            if not await self.client.collection_exists(name):
                return
            await self.client.delete(collection_name=name,
                                     points_selector=qm.FilterSelector(filter=self._filter(org_id, filters)), wait=True)
        except (ResponseHandlingException, UnexpectedResponse, OSError) as exc:
            raise _unavailable(exc) from exc

    async def delete_ids(self, name: str, ids: list[uuid.UUID]) -> None:
        if not ids:
            return
        try:
            if await self.client.collection_exists(name):
                await self.client.delete(collection_name=name,
                                         points_selector=qm.PointIdsList(points=[str(i) for i in ids]), wait=True)
        except (ResponseHandlingException, UnexpectedResponse, OSError) as exc:
            raise _unavailable(exc) from exc

    async def health(self) -> bool:
        try:
            await self.client.get_collections()
            return True
        except Exception:  # health probe: any failure means unavailable
            return False

    async def close(self) -> None:
        await self.client.close()
