"""Request and response bodies of R-1, R-6 and R-7 (§5.2, §5.5, §5.7.1, §5.7.6, §5.7.7).

Bounds follow §5.2 and answer ``400 VALIDATION_FAILED`` through the platform's handler.
"""

from __future__ import annotations

from typing import Literal, Self
from uuid import UUID

from pydantic import BaseModel, Field

from rag.application.retrieval.dtos import (
    ClassificationResult,
    RequiredInputResult,
    SearchResult,
)
from rag.application.retrieval.scope import ScopeSpec
from rag.domain.retrieval.sufficiency import GapReport
from rag.presentation.api.public.artefacts_models import Chunk, RetrievalVerdict

CollectionType = Literal["tender", "tenant", "reference"]


class ScopeBody(BaseModel):
    """``ScopeRequest`` (§5.5). ``tender_id`` routes and filters, never authorizes (API-3)."""

    tender_id: UUID | None = None
    collection_ids: list[UUID] = Field(default_factory=list, max_length=100)
    collection_types: list[CollectionType] = Field(default_factory=list, max_length=3)

    def to_spec(self, tender_id: UUID | None = None) -> ScopeSpec:
        """The application's scope request; an explicit ``tender_id`` argument wins."""
        return ScopeSpec(
            tender_id or self.tender_id, tuple(self.collection_ids), tuple(self.collection_types)
        )


class PriorRefinementBody(BaseModel):
    """``PriorRefinement`` (§5.5). Accepted; ``user_action`` capture is not built (§20 OQ-22)."""

    refinement_id: UUID
    action: Literal["accepted", "edited", "discarded"]


class SearchRequest(BaseModel):
    """R-1 body."""

    query: str = Field(min_length=1, max_length=4000)
    scope: ScopeBody = Field(default_factory=ScopeBody)
    section_type: str | None = Field(default=None, max_length=256)
    prior_refinement: PriorRefinementBody | None = None
    limit: int | None = Field(default=None, ge=1)


class RefinementBlock(BaseModel):
    """The ``refinement`` block of R-1; absent when the flag is off (REF-2)."""

    refinement_id: UUID
    intent: str | None
    refined_query: str | None
    exact_terms: list[str]
    original_query: str
    refiner_version: str
    outcome: str


class AppliedProfile(BaseModel):
    """``applied_profile`` of R-1."""

    source: str
    values_provisional: bool
    candidate_depth: int
    rerank_in: int
    rerank_out: int


class SearchResponse(BaseModel):
    """R-1: ``200``, or ``206`` where ``degradation_markers`` is non-empty."""

    verdict: RetrievalVerdict
    chunks: list[Chunk]
    refinement: RefinementBlock | None
    applied_profile: AppliedProfile

    @classmethod
    def of(cls, result: SearchResult) -> Self:
        """From the use case's result."""
        o = result.outcome
        refinement = None
        if o.refinement is not None:
            r = o.refinement
            refinement = RefinementBlock(
                refinement_id=r.id,
                intent=r.intent,
                refined_query=r.refined_query,
                exact_terms=list(r.exact_terms),
                original_query=r.original_query,
                refiner_version=r.refiner_version,
                outcome=r.outcome.value,
            )
        return cls(
            verdict=RetrievalVerdict(
                retrieval_set_id=result.retrieval_set_id,
                verdict=o.verdict.value,
                verdict_reason=o.verdict_reason,
                widened_retry_of=result.widened_retry_of,
                values_provisional=o.plan.values_provisional,
                degradation_markers=[m.as_dict() for m in o.markers],
            ),
            chunks=[
                Chunk(
                    rank=rank,
                    chunk_id=c.chunk_id,
                    parent_chunk_id=c.parent_chunk_id,
                    document_id=c.document_id,
                    document_version=c.document_version,
                    document_title=c.document_title,
                    heading_path=c.heading_path,
                    clause_ref=c.clause_ref,
                    document_role=c.document_role,
                    language=c.language,
                    collection_id=c.collection_id,
                    collection_weight=float(c.weight),
                    fused_score=c.fused_score,
                    rerank_score=c.rerank_score,
                    source_half=c.source_half,
                    excerpt=c.excerpt,
                    excerpt_is_translation=False,
                    source_span=None,
                    purged=False,
                )
                for rank, c in enumerate(o.chunks, start=1)
            ],
            refinement=refinement,
            applied_profile=AppliedProfile(**o.plan.applied_profile()),
        )


class ClassifyRequest(BaseModel):
    """R-6 body: 1 MB of caller-supplied content (§5.2)."""

    content: str = Field(min_length=1, max_length=1_000_000)
    filename: str | None = Field(default=None, max_length=256)
    domain_profile_ref: str | None = Field(default=None, max_length=256)


class RoleAlternativeBody(BaseModel):
    """One alternative."""

    role: str
    confidence: float


class ClassifyResponse(BaseModel):
    """R-6: stateless proposal (MD-21)."""

    proposed_role: str
    confidence: float
    alternatives: list[RoleAlternativeBody]
    taxonomy_version: str

    @classmethod
    def of(cls, result: ClassificationResult) -> Self:
        """From the use case's result."""
        return cls(
            proposed_role=result.proposed_role,
            confidence=result.confidence,
            alternatives=[
                RoleAlternativeBody(role=a.role, confidence=a.confidence)
                for a in result.alternatives
            ],
            taxonomy_version=result.taxonomy_version,
        )


class RequiredInputRequest(BaseModel):
    """R-7 body."""

    tender_id: UUID | None = None
    scope: ScopeBody = Field(default_factory=ScopeBody)
    capability: str | None = Field(default=None, max_length=256)
    section_types: list[str] = Field(min_length=1, max_length=100)


class GapReportBody(BaseModel):
    """``GapReport`` (§5.5)."""

    missing_roles: list[str]
    unresolved_entities: list[str]
    empty_collections: list[UUID]
    blocking: bool

    @classmethod
    def of(cls, gap: GapReport) -> Self:
        """From the domain value."""
        return cls(
            missing_roles=list(gap.missing_roles),
            unresolved_entities=list(gap.unresolved_entities),
            empty_collections=list(gap.empty_collections),
            blocking=gap.blocking,
        )


class RequiredInputResponse(BaseModel):
    """R-7: ``200`` on every outcome, a blocking gap included (§5.7.7)."""

    per_section: dict[str, GapReportBody]
    profile_version: str

    @classmethod
    def of(cls, result: RequiredInputResult) -> Self:
        """From the use case's result."""
        return cls(
            per_section={k: GapReportBody.of(v) for k, v in result.per_section.items()},
            profile_version=result.profile_version,
        )
