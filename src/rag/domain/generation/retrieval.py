"""Retrieval record (LLD 4.4): sets, chunks and purge markers."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

from rag.domain.generation.exceptions import InvariantViolation
from rag.domain.shared.enums import GrantLevel, RetrievalVerdict


@dataclass(frozen=True, slots=True)
class RetrievalSet:
    """What a replay needs to rerun a retrieval. Immutable after insert, purge included (RS-1)."""

    id: UUID
    tenant_id: UUID
    verdict: RetrievalVerdict
    scope_resolution_hash: str
    grant_level_applied: GrantLevel
    filter_set: dict[str, Any]
    index_generations: dict[str, Any]
    profile_source: str
    applied_profile: dict[str, Any]
    reranker_ran: bool
    budget_allocation: dict[str, Any]
    domain_profile_version: str
    language_pack_version: str
    tender_id: UUID | None = None
    artefact_id: UUID | None = None
    verdict_reason: str | None = None
    grant_level_clamped: bool = False
    values_provisional: bool = False
    query_refinement_id: UUID | None = None
    widened_retry_of: UUID | None = None
    degradation_markers: list[dict[str, Any]] = field(default_factory=list)
    created_at: datetime | None = None

    def __post_init__(self) -> None:
        if self.verdict is not RetrievalVerdict.SUFFICIENT and not self.verdict_reason:
            raise InvariantViolation("RS-5", "a non-sufficient verdict always carries a reason")


@dataclass(frozen=True, slots=True)
class RetrievalChunk:
    """One ranked chunk with its recorded provenance (4.4.2). ``chunk_id`` is text (RSP-3)."""

    retrieval_set_id: UUID
    tenant_id: UUID
    rank: int
    chunk_id: str
    document_id: UUID
    document_version: int
    collection_id: UUID
    collection_weight: Decimal
    fused_score: Decimal
    source_half: str
    document_title: str
    document_role: str
    language: str
    excerpt: str
    rerank_score: Decimal | None = None
    parent_chunk_id: str | None = None
    heading_path: str | None = None
    clause_ref: str | None = None
    excerpt_is_translation: bool = False
    source_span: dict[str, Any] | None = None

    def __post_init__(self) -> None:
        if self.rank < 1:
            raise InvariantViolation("RSC-2", "rank is dense and ordered from 1")


@dataclass(frozen=True, slots=True)
class RetrievalSetPurge:
    """A purge marker beside a set (4.4.3): never folded into the evidence tier (RSP-2)."""

    id: UUID
    retrieval_set_id: UUID
    tenant_id: UUID
    purged_document_id: UUID
    purged_version: int
    chunk_ids: tuple[str, ...]
    event_id: UUID
    purged_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class RetrievalSetView:
    """A recorded set with its ordered chunks and purge markers (R-5)."""

    retrieval_set: RetrievalSet
    chunks: tuple[RetrievalChunk, ...]
    purges: tuple[RetrievalSetPurge, ...]

    @property
    def purged_chunk_ids(self) -> frozenset[str]:
        """Chunk ids covered by any purge row; the chunk itself stays intact (RS-1)."""
        return frozenset(c for p in self.purges for c in p.chunk_ids)

    @property
    def collection_ids(self) -> tuple[UUID, ...]:
        """Every collection the set drew from, sorted: the scope R-5 re-resolves (API-10)."""
        return tuple(sorted({c.collection_id for c in self.chunks}))
