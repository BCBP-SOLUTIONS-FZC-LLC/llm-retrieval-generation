"""Request and response bodies of W-1 to W-7 and W-8 (§5.7.12 to §5.7.14).

Bounds follow §5.2 and answer ``400 VALIDATION_FAILED`` through the platform's handler.
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any, Literal, Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

from rag.application.events.contracts import Recipient
from rag.application.generation.regenerate import MAX_REVIEW_CONTEXT_CHARS
from rag.application.workflows.common import SignalAck
from rag.application.workflows.operator import Rematerialised
from rag.application.workflows.start import RunHandle
from rag.application.workflows.state import GenerationStateDTO
from rag.domain.shared.enums import GenerationMode
from rag.presentation.api.public.generation_models import EligibilityBody
from rag.presentation.api.public.retrieval_models import ScopeBody

Mode = Literal["assistance", "automation"]


class RecipientBody(BaseModel):
    """``{kind, id}`` echoed on the run's events (LD-83, provisional)."""

    kind: str = Field(min_length=1, max_length=64)
    id: str = Field(min_length=1, max_length=128)


def recipients_of(items: list[RecipientBody]) -> tuple[Recipient, ...]:
    """Body recipients as the application's."""
    return tuple(Recipient(r.kind, r.id) for r in items)


class StartGenerationRequest(BaseModel):
    """W-1 body."""

    tender_id: UUID
    capability: str = Field(min_length=1, max_length=256)
    target_ref: str | None = Field(default=None, max_length=256)
    scope: ScopeBody = Field(default_factory=ScopeBody)
    requested_mode: Mode = "assistance"
    outline_gate: bool = True
    recipients: list[RecipientBody] = Field(default_factory=list, max_length=50)

    def mode(self) -> GenerationMode:
        """The requested mode as the domain enum."""
        return GenerationMode(self.requested_mode)


class StartExtractionRequest(BaseModel):
    """W-6 body: W-1's shape with ``schema_ref`` in place of ``capability``."""

    tender_id: UUID
    schema_ref: str = Field(min_length=1, max_length=256)
    target_ref: str | None = Field(default=None, max_length=256)
    scope: ScopeBody = Field(default_factory=ScopeBody)
    trigger_document_version_id: str | None = Field(default=None, max_length=256)
    recipients: list[RecipientBody] = Field(default_factory=list, max_length=50)


class RunHandleResponse(BaseModel):
    """The ``202`` body of W-1, W-6 and A-5 (``200`` on a replay)."""

    generation_id: UUID
    artefact_id: UUID
    status: str
    eligibility: EligibilityBody
    domain_profile_version: str
    language_pack_version: str

    @classmethod
    def of(cls, h: RunHandle) -> Self:
        """From the use case's handle."""
        e = h.eligibility
        return cls(
            generation_id=h.generation_id,
            artefact_id=h.artefact_id,
            status=h.status.value,
            eligibility=EligibilityBody(
                state=e.state.value,
                requested_mode=e.requested_mode.value,
                effective_mode=e.effective_mode.value,
                clamped=e.clamped,
                clamp_reason=e.clamp_reason,
                profile_version=e.profile_version,
            ),
            domain_profile_version=h.domain_profile_version,
            language_pack_version=h.language_pack_version,
        )


class Out(BaseModel):
    """Base: validates straight from the application dataclasses."""

    model_config = ConfigDict(from_attributes=True)


class OutlineSection(Out):
    """One outline entry."""

    ref: str
    type: str


class Outline(Out):
    """Outline state and version."""

    state: str
    version: int | None
    sections: list[OutlineSection]
    ready_at: datetime | None
    gate_expires_at: datetime | None


class SectionProgress(Out):
    """A section, with the reason for its state."""

    ref: str
    state: str
    section_id: UUID
    section_version_id: UUID | None = None
    evidence_tier: str | None = None
    retrieval_set_id: UUID | None = None
    gap_reason: str | None = None
    failure_reason: str | None = None
    flagged_for_review: bool = False


class Coherence(Out):
    """The coherence verdict."""

    verdict: str
    computed_at: datetime | None


class Progress(Out):
    """A count over the section rows."""

    total: int
    complete: int
    gap: int
    failed: int


class GenerationStateResponse(Out):
    """W-2."""

    generation_id: UUID
    artefact_id: UUID
    status: str
    outline: Outline
    sections: list[SectionProgress]
    established_facts: dict[str, str]
    coherence: Coherence
    scope_narrowed: bool
    progress: Progress
    failure_reason: str | None = None
    failed_at_stage: str | None = None
    current_stage: str | None = None
    origin: str = "original"

    @classmethod
    def of(cls, dto: GenerationStateDTO) -> Self:
        """From the application DTO."""
        return cls.model_validate(dto)


class ApproveRequest(BaseModel):
    """W-3 body."""

    outline_version: int = Field(ge=1)


class OutlineEntryBody(BaseModel):
    """One section of an edited outline: a reference and a type, never content."""

    ref: str = Field(min_length=1, max_length=256)
    type: str = Field(min_length=1, max_length=256)


class ReviseRequest(BaseModel):
    """W-4 body."""

    outline_version: int = Field(ge=1)
    sections: list[OutlineEntryBody] = Field(default_factory=list, max_length=200)


class CancelRequest(BaseModel):
    """W-5 body: free text, at most 256 characters."""

    reason: str | None = Field(default=None, max_length=256)


class ResumeRequest(BaseModel):
    """W-7 body: ``{}``."""

    model_config = ConfigDict(extra="forbid")


class SignalResponse(BaseModel):
    """The ``202`` body of W-3, W-4, W-5 and W-7: the resulting workflow status."""

    generation_id: UUID
    artefact_id: UUID
    status: str
    outline_state: str
    outline_version: int | None

    @classmethod
    def of(cls, ack: SignalAck) -> Self:
        """From the use case's acknowledgement."""
        return cls(
            generation_id=ack.generation_id,
            artefact_id=ack.artefact_id,
            status=ack.status.value,
            outline_state=ack.outline_state.value,
            outline_version=ack.outline_version,
        )


class RematerialiseRequest(BaseModel):
    """W-8 body: ``ticket_ref`` is required and at most 128 characters."""

    ticket_ref: str = Field(min_length=1, max_length=128)
    reason: str | None = Field(default=None, max_length=512)


class RematerialiseResponse(BaseModel):
    """W-8's ``202``."""

    generation_id: UUID
    artefact_id: UUID
    workflow_id: str
    rematerialised_at: datetime

    @classmethod
    def of(cls, r: Rematerialised) -> Self:
        """From the use case's result."""
        return cls(
            generation_id=r.generation_id,
            artefact_id=r.artefact_id,
            workflow_id=r.workflow_id,
            rematerialised_at=r.rematerialised_at,
        )


class RegenerateDocumentRequest(BaseModel):
    """A-5 body. ``review_context`` is not accepted: the run's input is stored (REGEN-3)."""

    model_config = ConfigDict(extra="forbid")

    base_version_id: UUID
    instruction: str | None = Field(default=None, max_length=2000)
    recipients: list[RecipientBody] = Field(default_factory=list, max_length=50)


class RegenerateSectionRequest(BaseModel):
    """A-6 body (§5.7.9). ``review_context`` is prompt input for this request only (REGEN-3)."""

    base_version_id: UUID
    instruction: str | None = Field(default=None, max_length=2000)
    review_context: dict[str, Any] | None = None
    recipients: list[RecipientBody] = Field(default_factory=list, max_length=50)

    @field_validator("review_context")
    @classmethod
    def _bounded(cls, value: dict[str, Any] | None) -> dict[str, Any] | None:
        """The context is prompt input and counts against the user-layer budget; bound it early."""
        if value is not None and len(json.dumps(value)) > MAX_REVIEW_CONTEXT_CHARS:
            raise ValueError(f"review_context is over {MAX_REVIEW_CONTEXT_CHARS} characters")
        return value


class FactConflictResolutionRequest(BaseModel):
    """A-7 body."""

    conflict_id: UUID
    resolution: Literal["keep_established", "update_fact", "dismiss"]


class FactConflictResolutionResponse(BaseModel):
    """A-7's ``200``: nothing regenerates here; ``proceeds`` says the client may re-issue A-6."""

    conflict_id: UUID
    resolution: str
    proceeds: bool
    flagged_section_ids: list[UUID]
