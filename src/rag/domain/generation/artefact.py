"""Artefact (LLD 4.3.1) and document version (4.3.2)."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from uuid import UUID

from rag.domain.generation.exceptions import InvariantViolation
from rag.domain.shared.enums import (
    ArtefactArchiveState,
    ArtefactKind,
    ArtefactOrigin,
    ArtefactStatus,
    CompletenessVerdict,
    DeliveryState,
    EligibilityState,
    EvidenceTier,
    GenerationMode,
)
from rag.domain.shared.errors import DomainError, ErrorCode

S = ArtefactStatus
ARTEFACT_TRANSITIONS: dict[ArtefactStatus, frozenset[ArtefactStatus]] = {
    S.PENDING: frozenset({S.GENERATING, S.FAILED, S.CANCELLED}),
    S.GENERATING: frozenset({S.COMPLETE, S.FAILED, S.CANCELLED}),
    S.COMPLETE: frozenset({S.GENERATING}),
    S.CANCELLED: frozenset({S.GENERATING}),
    S.FAILED: frozenset({S.GENERATING}),
    S.SUPERSEDED: frozenset(),  # ART-5: reserved, nothing writes it and nothing leaves it
}
"""ART-5 status table. No target set contains ``SUPERSEDED``: no code may write it."""


@dataclass(slots=True)
class Artefact:
    """One generated thing's stable identity. No trigger guards ``status`` (ART-5): this does."""

    id: UUID
    tenant_id: UUID
    tender_id: UUID | None
    kind: ArtefactKind
    capability: str
    created_by: UUID
    status: ArtefactStatus = ArtefactStatus.PENDING
    tender_section_ref: str | None = None
    target_ref: str | None = None
    current_version_id: UUID | None = None
    archive_state: ArtefactArchiveState = ArtefactArchiveState.LIVE
    archived_at: datetime | None = None
    record_version: int = 1
    created_at: datetime | None = None
    updated_at: datetime | None = None

    def __post_init__(self) -> None:
        if self.tender_id is None and self.kind is not ArtefactKind.ANSWER:
            raise InvariantViolation("ART-1", "tender_id is NULL only where kind = 'answer'")
        if self.archive_state is ArtefactArchiveState.ARCHIVED and (
            self.archived_at is None or self.tender_id is None
        ):
            raise InvariantViolation("ART-7", "an archived artefact needs archived_at and a tender")

    @property
    def is_archived(self) -> bool:
        """Whether the artefact's tender closed (ART-7): reads work, new work is refused."""
        return self.archive_state is ArtefactArchiveState.ARCHIVED

    def require_writable(self) -> None:
        """ART-7: an archived artefact refuses new work with ``409 TENDER_CLOSED``."""
        if self.is_archived:
            raise DomainError(ErrorCode.TENDER_CLOSED, "The artefact's tender is closed.")

    def move_to(self, target: ArtefactStatus) -> None:
        """Apply an ART-5 transition or raise."""
        if target not in ARTEFACT_TRANSITIONS[self.status]:
            raise InvariantViolation("ART-5", f"{self.status} -> {target} is not a transition")
        self.status = target

    def archive(self, at: datetime) -> None:
        """ART-7 archive; ART-8: a tender-less artefact is never archived by a tender event."""
        if self.tender_id is None:
            raise InvariantViolation("ART-8", "an artefact with no tender is never archived")
        self.archive_state = ArtefactArchiveState.ARCHIVED
        self.archived_at = at


@dataclass(frozen=True, slots=True)
class ArtefactVersion:
    """A document-level version, immutable after insert except the supersession marker (AV-1)."""

    id: UUID
    artefact_id: UUID
    tenant_id: UUID
    version_no: int
    minted_reason: str
    evidence_tier: EvidenceTier
    completeness: CompletenessVerdict
    eligibility_state: EligibilityState
    requested_mode: GenerationMode
    effective_mode: GenerationMode
    domain_profile_version: str
    language_pack_version: str
    model_profile_id: str
    prompt_hash: str
    parser_version: str
    chunker_version: str
    generation_run_id: UUID
    delivery: DeliveryState
    origin: ArtefactOrigin = ArtefactOrigin.ORIGINAL
    superseded_by_section_edit: bool = False
    clamped: bool = False
    clamp_reason: str | None = None
    output_schema_version: str | None = None
    degradation_markers: list[dict[str, Any]] = field(default_factory=list)
    instruction: str | None = None
    instruction_refinement_id: UUID | None = None
    review_context_read: bool = False
    review_context_ref: str | None = None
    disclaimer_ref: str | None = None
    created_at: datetime | None = None

    def __post_init__(self) -> None:
        if self.version_no < 1:
            raise InvariantViolation("AV-1", "version_no must be positive")
        if self.clamped and not self.clamp_reason:
            raise InvariantViolation("AV-2", "a clamp is always recorded with its reason")
