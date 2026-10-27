"""Request and response bodies of R-2, R-3 and R-4 (§5.2, §5.7.2 to §5.7.4).

Bounds follow §5.2 and answer ``400 VALIDATION_FAILED`` through the platform's handler.
"""

from __future__ import annotations

from typing import Any, Literal, Self
from uuid import UUID

from pydantic import BaseModel, Field

from rag.application.generation.extract import ExtractionOutcome
from rag.domain.shared.enums import GenerationMode
from rag.presentation.api.public.retrieval_models import PriorRefinementBody, ScopeBody

Mode = Literal["assistance", "automation"]


class AnswerRequest(BaseModel):
    """R-2 body. ``scope.tender_id`` is optional: an answer may stand free of a tender."""

    question: str = Field(min_length=1, max_length=4000)
    scope: ScopeBody = Field(default_factory=ScopeBody)
    prior_refinement: PriorRefinementBody | None = None
    requested_mode: Mode = "assistance"

    def mode(self) -> GenerationMode:
        """The requested mode as the domain enum."""
        return GenerationMode(self.requested_mode)


class DraftRequest(BaseModel):
    """R-3 body. ``tender_id`` is required; ``tender_section_ref`` is opaque (ART-4)."""

    tender_id: UUID
    tender_section_ref: str = Field(min_length=1, max_length=256)
    section_type: str = Field(min_length=1, max_length=256)
    scope: ScopeBody = Field(default_factory=ScopeBody)
    user_prompt: str = Field(default="", max_length=8000)
    prior_refinement: PriorRefinementBody | None = None
    requested_mode: Mode = "assistance"

    def mode(self) -> GenerationMode:
        """The requested mode as the domain enum."""
        return GenerationMode(self.requested_mode)


class ExtractRequest(BaseModel):
    """R-4 body."""

    tender_id: UUID
    scope: ScopeBody = Field(default_factory=ScopeBody)
    schema_ref: str = Field(min_length=1, max_length=256)
    document_ids: list[UUID] = Field(default_factory=list, max_length=100)
    trigger_document_version_id: str | None = Field(default=None, max_length=256)


class EligibilityBody(BaseModel):
    """``EligibilityVerdict`` (§5.5, API-5)."""

    state: str
    requested_mode: str
    effective_mode: str
    clamped: bool
    clamp_reason: str | None
    profile_version: str


class ExtractionResponse(BaseModel):
    """R-4: ``200``, or ``206`` where ``degradation_markers`` is non-empty (§5.7.4)."""

    artefact_id: UUID
    generation_id: UUID
    artefact_version_id: UUID | None
    retrieval_set_id: UUID | None
    schema_version: str
    fields: dict[str, dict[str, Any]]
    evidence_tier: str
    eligibility: EligibilityBody
    degradation_markers: list[dict[str, str]]
    refusal: dict[str, str] | None = None

    @classmethod
    def of(cls, o: ExtractionOutcome) -> Self:
        """From the use case's outcome."""
        e = o.eligibility
        return cls(
            artefact_id=o.artefact_id,
            generation_id=o.generation_id,
            artefact_version_id=o.artefact_version_id,
            retrieval_set_id=o.retrieval_set_id,
            schema_version=o.schema_version,
            fields=o.fields,
            evidence_tier=o.evidence_tier.value,
            eligibility=EligibilityBody(
                state=e.state.value,
                requested_mode=e.requested_mode.value,
                effective_mode=e.effective_mode.value,
                clamped=e.clamped,
                clamp_reason=e.clamp_reason,
                profile_version=e.profile_version,
            ),
            degradation_markers=list(o.degradation_markers),
            refusal=o.refusal,
        )
