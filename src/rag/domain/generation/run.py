"""Generation run (LLD 4.3.6): status and outline machines, RUN-* invariants."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID

from rag.domain.generation.exceptions import InvariantViolation
from rag.domain.shared.enums import (
    ArtefactOrigin,
    CoherenceVerdict,
    EligibilityState,
    GenerationMode,
    OutlineState,
    RunFailureReason,
    RunStage,
    RunStatus,
)

R = RunStatus
RUN_TRANSITIONS: dict[RunStatus, frozenset[RunStatus]] = {
    R.PENDING: frozenset({R.OUTLINE_BUILDING, R.GENERATING, R.CANCELLED}),
    R.OUTLINE_BUILDING: frozenset({R.AWAITING_OUTLINE_APPROVAL, R.CANCELLED}),
    R.AWAITING_OUTLINE_APPROVAL: frozenset(
        {R.GENERATING, R.OUTLINE_BUILDING, R.FAILED, R.CANCELLED}
    ),
    R.GENERATING: frozenset({R.SUSPENDED, R.COMPLETE, R.FAILED, R.CANCELLED}),
    R.SUSPENDED: frozenset({R.GENERATING, R.CANCELLED}),
    R.COMPLETE: frozenset(),
    R.FAILED: frozenset(),
    R.CANCELLED: frozenset(),
}
"""The run's own state machine (4.3.6). ``complete``, ``failed``, ``cancelled`` are terminal."""

SCOPE_REVOCABLE = frozenset(
    {R.PENDING, R.OUTLINE_BUILDING, R.AWAITING_OUTLINE_APPROVAL, R.GENERATING, R.SUSPENDED}
)
"""ASYNC-15: a signal route whose re-resolution is empty ends the run ``scope_revoked`` from any
non-terminal status. §4.3.6's table lists no such edges (it has ``awaiting_outline_approval ->
failed`` for gate expiry only); ASYNC-15 and LD-62 require them, so they are written here."""

O = OutlineState  # noqa: E741 - mirrors the LLD's machine name
OUTLINE_TRANSITIONS: dict[OutlineState, frozenset[OutlineState]] = {
    O.NONE: frozenset({O.BUILDING}),
    O.BUILDING: frozenset({O.READY}),
    O.READY: frozenset({O.APPROVED, O.REVISING}),
    O.REVISING: frozenset({O.BUILDING}),
    O.APPROVED: frozenset(),
}


@dataclass(slots=True)
class GenerationRun:
    """One generation attempt, written at admission before anything is dispatched (RUN-1)."""

    id: UUID
    artefact_id: UUID
    tenant_id: UUID
    principal_id: UUID
    requested_mode: GenerationMode
    effective_mode: GenerationMode
    eligibility_state: EligibilityState
    domain_profile_version: str
    language_pack_version: str
    tender_id: UUID | None = None
    origin: ArtefactOrigin = ArtefactOrigin.ORIGINAL
    base_version_id: UUID | None = None
    workflow_id: str | None = None
    run_input: dict[str, Any] | None = None
    current_stage: RunStage | None = None
    idempotency_key: str | None = None
    request_hash: str | None = None
    status: RunStatus = RunStatus.PENDING
    failure_reason: RunFailureReason | None = None
    failed_at_stage: RunStage | None = None
    clamped: bool = False
    clamp_reason: str | None = None
    outline_state: OutlineState = OutlineState.NONE
    outline_version: int | None = None
    outline_ready_at: datetime | None = None
    gate_expires_at: datetime | None = None
    coherence: CoherenceVerdict = CoherenceVerdict.NOT_COMPUTED
    coherence_computed_at: datetime | None = None
    record_version: int = 1
    created_at: datetime | None = None
    updated_at: datetime | None = None

    def __post_init__(self) -> None:
        if self.clamped and not self.clamp_reason:
            raise InvariantViolation("RUN-1", "a clamp is recorded with its reason (ck_run_clamp)")
        if self.status is R.FAILED and self.failure_reason is None:
            raise InvariantViolation("RUN-5", "a failed run always carries a reason")
        if self.origin is not ArtefactOrigin.ORIGINAL and self.base_version_id is None:
            raise InvariantViolation("RUN-1", "a regeneration names its base version")
        if self.idempotency_key is not None and self.request_hash is None:
            raise InvariantViolation("RUN-3", "an idempotency key carries its request hash")
        if self.outline_state is not O.NONE and self.outline_version is None:
            raise InvariantViolation("RUN-7", "an outline state other than none needs a version")

    @property
    def is_terminal(self) -> bool:
        """Whether no transition leaves the run's status."""
        return not RUN_TRANSITIONS[self.status]

    def move_to(self, target: RunStatus) -> None:
        """Apply a run-status edge or raise."""
        if target not in RUN_TRANSITIONS[self.status]:
            raise InvariantViolation("RUN-7", f"{self.status} -> {target} is not a run transition")
        self.status = target

    def fail(self, reason: RunFailureReason, stage: RunStage | None = None) -> None:
        """Terminal failure (RUN-5): a reason is mandatory."""
        self.move_to(R.FAILED)
        self.failure_reason = reason
        self.failed_at_stage = stage

    def _outline(self, target: OutlineState) -> None:
        if target not in OUTLINE_TRANSITIONS[self.outline_state]:
            raise InvariantViolation(
                "RUN-7", f"outline {self.outline_state} -> {target} is not a transition"
            )
        self.outline_state = target

    def outline_started(self) -> None:
        """The outline activity runs: ``none -> building`` with ``pending -> outline_building``.

        On a revision (``revising -> building``) the run is already ``outline_building``: W-4
        moved it, so only the outline state moves here.
        """
        if self.outline_state is O.NONE:
            self.outline_version = self.outline_version or 1
            self.move_to(R.OUTLINE_BUILDING)
        self._outline(O.BUILDING)

    def outline_written(self, at: datetime, version: int | None = None) -> None:
        """``building -> ready``; the run awaits approval."""
        self._outline(O.READY)
        self.outline_version = version or self.outline_version
        self.outline_ready_at = at
        self.move_to(R.AWAITING_OUTLINE_APPROVAL)

    def approve_outline(self) -> None:
        """W-3: ``ready -> approved``, run to ``generating`` at stage ``stage1``."""
        self.move_to(R.GENERATING)
        self._outline(O.APPROVED)
        self.current_stage = RunStage.STAGE1

    def revise_outline(self) -> None:
        """W-4: ``ready -> revising``, ``outline_version`` + 1 (by this service only, RUN-7)."""
        self.move_to(R.OUTLINE_BUILDING)
        self._outline(O.REVISING)
        self.outline_version = (self.outline_version or 0) + 1
        self.current_stage = RunStage.OUTLINE

    def cancel(self) -> None:
        """W-5: any non-terminal status to ``cancelled`` (completed sections stay durable)."""
        self.move_to(R.CANCELLED)

    def resume(self) -> None:
        """W-7: ``suspended -> generating``. The suspension's reason is spent and is cleared."""
        self.move_to(R.GENERATING)
        self.failure_reason = None

    def revoke_scope(self) -> None:
        """ASYNC-15 / LD-62: the run ends ``failed`` with ``scope_revoked`` at its current stage."""
        if self.status not in SCOPE_REVOCABLE:
            raise InvariantViolation("RUN-7", f"{self.status} has no scope_revoked edge")
        self.status = R.FAILED
        self.failure_reason = RunFailureReason.SCOPE_REVOKED
        self.failed_at_stage = self.current_stage
