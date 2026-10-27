"""Application read models for A-1 to A-4, A-9 and R-5: plain data, no framework types."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from uuid import UUID


@dataclass(frozen=True, slots=True)
class EligibilityDTO:
    """``EligibilityVerdict`` (5.5)."""

    state: str
    requested_mode: str
    effective_mode: str
    clamped: bool
    clamp_reason: str | None
    profile_version: str


@dataclass(frozen=True, slots=True)
class VersionSummaryDTO:
    """A version without its verification records (API-8)."""

    id: UUID
    version_no: int
    minted_reason: str
    origin: str
    evidence_tier: str
    completeness: str
    delivery: str
    eligibility: EligibilityDTO
    superseded_by_section_edit: bool
    created_at: datetime | None


@dataclass(frozen=True, slots=True)
class ArtefactDTO:
    """A-1."""

    id: UUID
    tender_id: UUID | None
    kind: str
    capability: str
    tender_section_ref: str | None
    target_ref: str | None
    status: str
    archive_state: str
    archived_at: datetime | None
    created_by: UUID
    current_run_id: UUID | None
    record_version: int
    created_at: datetime | None
    updated_at: datetime | None
    current_version: VersionSummaryDTO | None


@dataclass(frozen=True, slots=True)
class PageDTO[T]:
    """A list response echoing the applied window."""

    items: list[T]
    total: int
    limit: int
    offset: int


@dataclass(frozen=True, slots=True)
class CitationDTO:
    """``Citation`` (5.5)."""

    claim_ordinal: int
    outcome: str
    cited_chunk_ids: list[str]
    corrected_chunk_ids: list[str] | None
    tier_decided: int


@dataclass(frozen=True, slots=True)
class GroundednessDTO:
    """Per-section groundedness (A-3)."""

    supported: int
    unsupported: int
    miscited: int
    gaps: int
    tiers_run: list[int]
    claims: list[CitationDTO]


@dataclass(frozen=True, slots=True)
class CompletenessDetailDTO:
    """Per-section completeness (A-3)."""

    items_total: int
    items_addressed: int
    missed_items: list[str]


@dataclass(frozen=True, slots=True)
class VersionSectionDTO:
    """One section of a document version (A-3)."""

    section_ref: str | None
    section_version_id: UUID
    evidence_tier: str
    completeness: str
    retrieval_set_id: UUID
    groundedness: GroundednessDTO | None
    completeness_detail: CompletenessDetailDTO | None


@dataclass(frozen=True, slots=True)
class VersionDetailDTO:
    """A-3: one version in full."""

    id: UUID
    version_no: int
    origin: str
    generation_run_id: UUID
    evidence_tier: str
    completeness: str
    sections: list[VersionSectionDTO]
    model_profile_id: str
    prompt_hash: str
    domain_profile_version: str
    language_pack_version: str
    parser_version: str
    chunker_version: str
    instruction: str | None
    instruction_refinement_id: UUID | None
    review_context_read: bool
    review_context_ref: str | None
    degradation_markers: list[dict[str, Any]]
    delivery: str
    eligibility: EligibilityDTO
    superseded_by_section_edit: bool
    disclaimer_ref: str | None


@dataclass(frozen=True, slots=True)
class SectionStateDTO:
    """A-4: one section's state and current-version tier."""

    section_id: UUID
    section_ref: str | None
    ordinal: int
    section_type: str
    is_foundation: bool
    state: str
    gap_reason: str | None
    failure_reason: str | None
    section_version_id: UUID | None
    evidence_tier: str | None
    flagged_for_review: bool
    flagged_reason: str | None


@dataclass(frozen=True, slots=True)
class SectionContentDTO:
    """A-9."""

    section_version_id: UUID
    version_no: int
    origin: str
    content_type: str
    content: str
    retrieval_set_id: UUID
    evidence_tier: str


@dataclass(frozen=True, slots=True)
class ChunkDTO:
    """``Chunk`` (5.5)."""

    rank: int
    chunk_id: str
    parent_chunk_id: str | None
    document_id: UUID
    document_version: int
    document_title: str
    heading_path: str | None
    clause_ref: str | None
    document_role: str
    language: str
    collection_id: UUID
    collection_weight: float
    fused_score: float
    rerank_score: float | None
    source_half: str
    excerpt: str
    excerpt_is_translation: bool
    source_span: dict[str, Any] | None
    purged: bool


@dataclass(frozen=True, slots=True)
class PurgeDTO:
    """A purge marker, beside the set and never merged into it (RSP-2)."""

    purged_document_id: UUID
    purged_version: int
    chunk_ids: list[str]
    purged_at: datetime | None


@dataclass(frozen=True, slots=True)
class RetrievalVerdictDTO:
    """``RetrievalVerdict`` (5.5)."""

    retrieval_set_id: UUID
    verdict: str
    verdict_reason: str | None
    widened_retry_of: UUID | None
    values_provisional: bool
    degradation_markers: list[dict[str, Any]]


@dataclass(frozen=True, slots=True)
class RetrievalSetDTO:
    """R-5."""

    id: UUID
    artefact_id: UUID | None
    tender_id: UUID | None
    verdict: RetrievalVerdictDTO
    scope_resolution_hash: str
    grant_level_applied: str
    grant_level_clamped: bool
    filter_set: dict[str, Any]
    index_generations: dict[str, Any]
    applied_profile: dict[str, Any]
    profile_source: str
    budget_allocation: dict[str, Any]
    reranker_ran: bool
    domain_profile_version: str
    language_pack_version: str
    query_refinement_id: UUID | None
    chunks: list[ChunkDTO] = field(default_factory=list)
    purges: list[PurgeDTO] = field(default_factory=list)
