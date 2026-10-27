"""Fail-closed stand-ins for ports whose real implementation is a later slice.

``corpusscope`` (scope resolution and every Qdrant read, DEP-1) does not exist yet. Until it
does, the scope resolver sees nothing and the point count refuses, so P-1/P-2/P-3/P-6 answer
empty or 404 and C-I8's ``complete`` answers 503: never a default scope, never a guessed count
(CG-3, CG-4).
"""

from __future__ import annotations

from uuid import UUID

from rag.domain.corpus.collection import Collection
from rag.domain.shared.errors import DomainError, ErrorCode
from rag.domain.shared.identity import RequestIdentity


class FailClosedScopeResolver:
    """A ``ScopeResolver`` that resolves no collection."""

    async def visible_collection_ids(
        self, identity: RequestIdentity, *, only: UUID | None = None
    ) -> tuple[UUID, ...]:
        """Always empty."""
        return ()


class UnavailablePointCounter:
    """A ``PointCounter`` with no Qdrant reader behind it."""

    async def count_tender_points(self, source: Collection) -> int:
        """Always ``503 SCOPE_STORE_UNAVAILABLE``."""
        raise DomainError(
            ErrorCode.SCOPE_STORE_UNAVAILABLE, "The vector store reader is not available."
        )
