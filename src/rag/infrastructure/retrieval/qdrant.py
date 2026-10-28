"""Dense search and entity counts over Qdrant, through ``corpusscope`` only (DEP-1, SCOPE-1)."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from rag.application.retrieval.ports import SearchHalfError
from rag.corpusscope import DenseHit, QdrantScopedReader, ScopeToken
from rag.domain.retrieval.candidate import Candidate
from rag.domain.retrieval.plan import StructuralFilters
from rag.domain.retrieval.scope import ResolvedScope
from rag.infrastructure.resilience import CircuitBreaker

SEARCH_TIMEOUT = 0.2  # §14.3: Qdrant search and the entity count, no retry


def _token(scope: ResolvedScope) -> ScopeToken:
    handle = scope.handle
    if not isinstance(handle, ScopeToken):
        raise SearchHalfError("the scope carries no token")
    return handle


def to_candidate(hit: DenseHit, collection_id: UUID, weight: Any) -> Candidate | None:
    """A point as a candidate; a point missing a field §4.10 requires is dropped, not guessed."""
    p = hit.payload
    try:
        return Candidate(
            chunk_id=str(p.get("chunk_id") or hit.point_id),
            document_id=UUID(str(p["document_id"])),
            document_version=int(p["document_version"]),
            collection_id=collection_id,
            text=str(p["text"]),
            document_title=str(p["document_title"]),
            document_role=str(p["document_role"]),
            language=str(p.get("language") or "und"),
            weight=weight,
            heading_path=p.get("heading_path"),
            clause_ref=p.get("clause_ref"),
            parent_chunk_id=p.get("parent_chunk_id"),
            source_half="dense",
        )
    except KeyError, ValueError, TypeError:
        return None


class QdrantDenseSearch:
    """``DenseSearch``: one search per searchable collection, concurrent, behind the breaker.

    Any failure is ``SearchHalfError`` (the pipeline falls to lexical-only).
    """

    def __init__(
        self,
        reader: QdrantScopedReader | None,
        breaker: CircuitBreaker,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._reader, self._breaker, self._clock = reader, breaker, clock

    async def search(
        self,
        scope: ResolvedScope,
        vector: list[float],
        *,
        limit: int,
        filters: StructuralFilters,
    ) -> list[Candidate]:
        """Top ``limit`` current points, best first."""
        reader = self._reader
        if reader is None:
            raise SearchHalfError("the vector store is not configured")
        token, now, weights = _token(scope), self._clock(), scope.weights()

        async def fan_out() -> list[tuple[UUID, list[DenseHit]]]:
            async def one(cid: UUID) -> tuple[UUID, list[DenseHit]]:
                hits = await reader.dense_search(
                    token,
                    cid,
                    vector,
                    limit,
                    now=now,
                    document_roles=filters.document_roles,
                    language=filters.language,
                )
                return cid, hits

            return list(await asyncio.gather(*(one(c.id) for c in scope.searchable)))

        try:
            per_collection = await self._breaker.call(
                lambda: asyncio.wait_for(fan_out(), SEARCH_TIMEOUT)
            )
        except Exception as exc:
            raise SearchHalfError("dense search failed") from exc
        scored: list[tuple[float, Candidate]] = []
        for cid, hits in per_collection:
            for hit in hits:
                cand = to_candidate(hit, cid, weights[cid])
                if cand is not None:
                    scored.append((hit.score, cand))
        scored.sort(key=lambda t: (-t[0], t[1].chunk_id))
        return [c for _, c in scored[:limit]]


class QdrantPointInspector:
    """``PointInspector``: scoped counts for the required-input check (§6.9).

    The 200 ms timeout and the Qdrant breaker are the dense search's (§14.3).
    """

    def __init__(
        self,
        reader: QdrantScopedReader | None,
        breaker: CircuitBreaker,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._reader, self._breaker, self._clock = reader, breaker, clock

    async def count(
        self,
        scope: ResolvedScope,
        *,
        entity_type: str | None = None,
        document_role: str | None = None,
    ) -> int:
        """Current points of that type or role; ``SearchHalfError`` where the count cannot run."""
        reader = self._reader
        if reader is None:
            raise SearchHalfError("the vector store is not configured")
        token, now = _token(scope), self._clock()

        async def total() -> int:
            counts = await asyncio.gather(
                *(
                    reader.count(
                        token, c.id, now=now, entity_type=entity_type, document_role=document_role
                    )
                    for c in scope.searchable
                )
            )
            return sum(counts)

        try:
            return await self._breaker.call(lambda: asyncio.wait_for(total(), SEARCH_TIMEOUT))
        except Exception as exc:
            raise SearchHalfError("point count failed") from exc
