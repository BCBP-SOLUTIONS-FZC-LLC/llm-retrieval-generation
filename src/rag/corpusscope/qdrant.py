"""The only Qdrant client calls of the platform (DEP-1). Each takes a token and a collection."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from uuid import UUID

from qdrant_client import AsyncQdrantClient

from rag.corpusscope.filters import count_filter, search_filter
from rag.corpusscope.token import ScopeToken


@dataclass(frozen=True, slots=True)
class DenseHit:
    """One point returned by a dense search: its score and the payload fields §4.10 lists."""

    point_id: str
    score: float
    payload: dict[str, Any] = field(default_factory=dict)


class QdrantScopedReader:
    """Dense search and counts, every one behind a filter built from a ``ScopeToken``."""

    def __init__(self, client: AsyncQdrantClient) -> None:
        self._client = client

    @classmethod
    def connect(cls, url: str, api_key: str | None = None) -> QdrantScopedReader:
        """A reader over a new client (lazy: no I/O until the first call)."""
        return cls(AsyncQdrantClient(url=url, api_key=api_key, check_compatibility=False))

    async def aclose(self) -> None:
        """Close the client."""
        await self._client.close()

    async def dense_search(
        self,
        token: ScopeToken,
        collection_id: UUID,
        vector: list[float],
        limit: int,
        *,
        now: datetime,
        document_roles: tuple[str, ...] = (),
        language: str | None = None,
    ) -> list[DenseHit]:
        """Top ``limit`` current points of one collection's active generation."""
        c = token.collection(collection_id)
        flt = search_filter(
            token, collection_id, now=now, document_roles=document_roles, language=language
        )
        response = await self._client.query_points(
            collection_name=c.physical_collection,
            query=vector,
            query_filter=flt,
            limit=limit,
            with_payload=True,
            shard_key_selector=c.shard_key,
        )
        return [DenseHit(str(p.id), float(p.score), dict(p.payload or {})) for p in response.points]

    async def count(
        self,
        token: ScopeToken,
        collection_id: UUID,
        *,
        now: datetime,
        entity_type: str | None = None,
        document_role: str | None = None,
    ) -> int:
        """Exact count of current points matching the type or role (§6.9, RET-8)."""
        c = token.collection(collection_id)
        flt = count_filter(
            token, collection_id, now=now, entity_type=entity_type, document_role=document_role
        )
        result = await self._client.count(
            collection_name=c.physical_collection,
            count_filter=flt,
            exact=True,
            shard_key_selector=c.shard_key,
        )
        return int(result.count)
