"""Artefact section (LLD 4.3.4) and section version (4.3.5)."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from uuid import UUID

from rag.domain.generation.exceptions import InvariantViolation
from rag.domain.shared.enums import (
    ArtefactOrigin,
    CompletenessVerdict,
    EvidenceTier,
    RunFailureReason,
    SectionState,
)

SS = SectionState
SECTION_TRANSITIONS: dict[SectionState, frozenset[SectionState]] = {
    SS.PENDING: frozenset({SS.GENERATING, SS.GAP}),
    SS.GENERATING: frozenset({SS.COMPLETE, SS.GAP, SS.FAILED}),
    SS.COMPLETE: frozenset({SS.GENERATING}),
    SS.GAP: frozenset({SS.GENERATING}),
    SS.FAILED: frozenset({SS.GENERATING}),
}
"""AS-5, identical to ``reject_section_transition`` in ``0002_generation_tables``."""

MAX_AUTO_RETRIES = 2
"""ASV-2: the HLD 8.5 ceiling."""


@dataclass(slots=True)
class ArtefactSection:
    """A section's stable identity and state. ``is_foundation`` is pack data (AS-1)."""

    id: UUID
    artefact_id: UUID
    tenant_id: UUID
    section_type: str
    ordinal: int
    tender_section_ref: str | None = None
    is_foundation: bool = False
    state: SectionState = SectionState.PENDING
    gap_reason: str | None = None
    failure_reason: RunFailureReason | None = None
    current_version_id: UUID | None = None
    flagged_for_review: bool = False
    flagged_reason: str | None = None
    record_version: int = 1
    created_at: datetime | None = None
    updated_at: datetime | None = None

    def __post_init__(self) -> None:
        if self.state is SS.GAP and not self.gap_reason:
            raise InvariantViolation("AS-2", "a section in state gap always carries a reason")
        if self.state is SS.FAILED and self.failure_reason is None:
            raise InvariantViolation("AS-5", "a failed section always carries a failure_reason")
        if self.state in (SS.GAP, SS.FAILED) and self.current_version_id is not None:
            raise InvariantViolation(
                "AS-7", "a section that has a current version is never gap or failed"
            )

    def _move(self, target: SectionState) -> None:
        if target not in SECTION_TRANSITIONS[self.state]:
            raise InvariantViolation("AS-5", f"{self.state} -> {target} is not a transition")
        # AS-5: leaving gap / failed clears the reason that required it.
        if self.state is SS.GAP:
            self.gap_reason = None
        if self.state is SS.FAILED:
            self.failure_reason = None
        self.state = target

    def start(self) -> None:
        """``pending|gap|failed|complete -> generating``."""
        self._move(SS.GENERATING)

    def complete(self, version_id: UUID | None = None) -> None:
        """``generating -> complete``; ``version_id`` is the newly written version, if any."""
        self._move(SS.COMPLETE)
        if version_id is not None:
            self.current_version_id = version_id

    def block(self, reason: str) -> None:
        """``pending -> gap`` on a required-input block (``required_input_missing``)."""
        self._move(SS.GAP)
        self.gap_reason = reason

    def end_without_version(self, *, gap_reason: str | None, failure: RunFailureReason) -> None:
        """A generating section whose attempt produced no version (AS-7).

        With a current version the section returns to ``complete`` and the previous version
        stays current; without one it becomes ``gap`` (when a content reason is given) or
        ``failed``.
        """
        if self.current_version_id is not None:
            self._move(SS.COMPLETE)
        elif gap_reason is not None:
            self._move(SS.GAP)
            self.gap_reason = gap_reason
        else:
            self._move(SS.FAILED)
            self.failure_reason = failure

    def close_in_flight(self, failure: RunFailureReason) -> None:
        """AS-8: the run-ending transaction closes a ``generating`` section; others stay put."""
        if self.state is SS.GENERATING:
            self.end_without_version(gap_reason=None, failure=failure)


@dataclass(frozen=True, slots=True)
class SectionVersion:
    """A section's real unit of change; never updated in place (ASV-1)."""

    id: UUID
    section_id: UUID
    tenant_id: UUID
    version_no: int
    content_ref: str
    content_sha256: bytes
    retrieval_set_id: UUID
    evidence_tier: EvidenceTier
    completeness: CompletenessVerdict
    model_profile_id: str
    prompt_hash: str
    serving_tier: str
    origin: ArtefactOrigin = ArtefactOrigin.ORIGINAL
    scope_narrowed: bool = False
    auto_retry_count: int = 0
    manual_regen_count: int = 0
    degradation_markers: list[dict[str, Any]] = field(default_factory=list)
    created_at: datetime | None = None

    def __post_init__(self) -> None:
        if self.version_no < 1:
            raise InvariantViolation("ASV-1", "version_no must be positive")
        if not 0 <= self.auto_retry_count <= MAX_AUTO_RETRIES:
            raise InvariantViolation("ASV-2", f"auto_retry_count is capped at {MAX_AUTO_RETRIES}")
        if len(self.content_sha256) != 32:
            raise InvariantViolation("ASV-4", "content_sha256 is a SHA-256 digest (32 bytes)")
