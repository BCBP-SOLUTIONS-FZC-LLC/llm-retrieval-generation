"""Response models of A-1 to A-4, A-9 and R-5 (LLD 5.5, 5.7). Built from the application DTOs."""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict


class Out(BaseModel):
    """Base: validates straight from the application dataclasses."""

    model_config = ConfigDict(from_attributes=True)


class Eligibility(Out):
    """``EligibilityVerdict``."""

    state: str
    requested_mode: str
    effective_mode: str
    clamped: bool
    clamp_reason: str | None
    profile_version: str


class VersionSummary(Out):
    """A version without verification records (API-8)."""

    id: UUID
    version_no: int
    minted_reason: str
    origin: str
    evidence_tier: str
    completeness: str
    delivery: str
    eligibility: Eligibility
    superseded_by_section_edit: bool
    created_at: datetime | None


class ArtefactResponse(Out):
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
    current_version: VersionSummary | None


class VersionPage(Out):
    """A-2."""

    items: list[VersionSummary]
    total: int
    limit: int
    offset: int


class Citation(Out):
    """``Citation``."""

    claim_ordinal: int
    outcome: str
    cited_chunk_ids: list[str]
    corrected_chunk_ids: list[str] | None
    tier_decided: int


class Groundedness(Out):
    """Per-section groundedness."""

    supported: int
    unsupported: int
    miscited: int
    gaps: int
    tiers_run: list[int]
    claims: list[Citation]


class CompletenessDetail(Out):
    """Per-section completeness."""

    items_total: int
    items_addressed: int
    missed_items: list[str]


class VersionSection(Out):
    """One section of a document version."""

    section_ref: str | None
    section_version_id: UUID
    evidence_tier: str
    completeness: str
    retrieval_set_id: UUID
    groundedness: Groundedness | None
    completeness_detail: CompletenessDetail | None


class VersionDetailResponse(Out):
    """A-3."""

    id: UUID
    version_no: int
    origin: str
    generation_run_id: UUID
    evidence_tier: str
    completeness: str
    sections: list[VersionSection]
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
    eligibility: Eligibility
    superseded_by_section_edit: bool
    disclaimer_ref: str | None


class SectionState(Out):
    """A-4 item."""

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


class SectionPage(Out):
    """A-4."""

    items: list[SectionState]
    total: int
    limit: int
    offset: int


class SectionContentResponse(Out):
    """A-9."""

    section_version_id: UUID
    version_no: int
    origin: str
    content_type: str
    content: str
    retrieval_set_id: UUID
    evidence_tier: str


class Chunk(Out):
    """``Chunk``."""

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


class Purge(Out):
    """A purge marker (RSP-2)."""

    purged_document_id: UUID
    purged_version: int
    chunk_ids: list[str]
    purged_at: datetime | None


class RetrievalVerdict(Out):
    """``RetrievalVerdict``."""

    retrieval_set_id: UUID
    verdict: str
    verdict_reason: str | None
    widened_retry_of: UUID | None
    values_provisional: bool
    degradation_markers: list[dict[str, Any]]


class RetrievalSetResponse(Out):
    """R-5."""

    id: UUID
    artefact_id: UUID | None
    tender_id: UUID | None
    verdict: RetrievalVerdict
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
    chunks: list[Chunk]
    purges: list[Purge]
