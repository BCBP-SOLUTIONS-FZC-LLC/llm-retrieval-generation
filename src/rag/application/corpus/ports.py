"""Outbound ports of the corpus module (in addition to the domain's repository ports)."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol
from uuid import UUID

from rag.application.corpus.dtos import ModelCatalogueDto
from rag.domain.corpus.collection import Collection
from rag.domain.corpus.repositories import (
    CollectionRepository,
    GrantRepository,
    IndexGenerationRepository,
    RetentionRepository,
    TenantLifecycleRepository,
)
from rag.domain.shared.identity import RequestIdentity

Clock = Callable[[], datetime]


def utc_now() -> datetime:
    """The default ``Clock``."""
    return datetime.now(UTC)


@dataclass(frozen=True, slots=True)
class CorpusRepositories:
    """The five repositories of ``llm_corpus``; all are used with one transaction handle."""

    collections: CollectionRepository
    generations: IndexGenerationRepository
    grants: GrantRepository
    retention: RetentionRepository
    lifecycle: TenantLifecycleRepository


class ScopeResolver(Protocol):
    """Which collections the caller may see (§6.2 scope resolution; ``corpusscope`` implements it).

    P-1, P-2, P-3 and P-6 find a collection only through this port, so an absent and an
    ungranted collection are both ``404`` (CG-8). Status filtering (COL-10) is the resolver's.
    """

    async def visible_collection_ids(
        self, identity: RequestIdentity, *, only: UUID | None = None
    ) -> tuple[UUID, ...]:
        """Ids in the caller's resolved scope; with ``only``, ``()`` or ``(only,)``."""
        ...


class PointCounter(Protocol):
    """Counts a tender's points in its source collection (Qdrant read, §5.7.16 C-I8)."""

    async def count_tender_points(self, source: Collection) -> int:
        """Points of ``source.tender_id`` in ``source``'s physical collection."""
        ...


class ModelCatalogue(Protocol):
    """The Model Gateway's catalogue; this service holds no model list of its own (C-M1)."""

    async def list_models(self) -> ModelCatalogueDto:
        """Raises ``DomainError(INFERENCE_UNAVAILABLE)`` where the catalogue cannot be read."""
        ...
