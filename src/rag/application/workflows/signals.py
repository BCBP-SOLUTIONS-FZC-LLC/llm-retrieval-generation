"""W-3, W-4, W-5 and W-7: the gate, cancel and resume routes (LLD §5.7.14, §9.4, §13.11).

Each one re-resolves scope (and, W-5 excepted, eligibility) first (ASYNC-4), then writes the run
row in one guarded transaction and raises its signal after that transaction commits (LD-88).
The row is the authority and the signal is the nudge: a repeat of an action whose row already
reads the post-state re-raises the signal, so a signal lost after the commit is not stranded.
"""

from __future__ import annotations

import copy
import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from uuid import UUID

from rag.application.events.contracts import GenerationCancelled, build_event, tender_subject
from rag.application.shared.ports import TxHandle
from rag.application.workflows.common import (
    RunActions,
    SignalAck,
    not_found,
    recipients_of,
)
from rag.application.workflows.ports import (
    SIGNAL_APPROVE,
    SIGNAL_CANCEL,
    SIGNAL_RESUME,
    SIGNAL_REVISE,
)
from rag.domain.generation import audit
from rag.domain.generation.run import GenerationRun
from rag.domain.shared.enums import (
    ArtefactStatus,
    AuditActorType,
    RunFailureReason,
    RunStatus,
)
from rag.domain.shared.errors import DomainError, ErrorCode
from rag.domain.shared.identity import RequestIdentity
from rag.domain.workflows import gate

log = logging.getLogger(__name__)
MAX_OUTLINE_SECTIONS = 200
MAX_REF_CHARS = 256
MAX_REASON_CHARS = 256


@dataclass(frozen=True, slots=True)
class OutlineEntry:
    """One section of an edited outline: a reference and a type, never content (ASYNC-12)."""

    ref: str
    type: str


@dataclass(frozen=True, slots=True)
class OutlineEdit:
    """W-4's body after validation."""

    outline_version: int
    sections: tuple[OutlineEntry, ...] = field(default_factory=tuple)


class _Gate:
    """The shared W-3 / W-4 / W-7 shape: guarded transition, progress event, then the signal."""

    def __init__(self, actions: RunActions) -> None:
        self._a = actions

    async def _transition(
        self,
        run_id: UUID,
        apply: Callable[[GenerationRun], None],
        *,
        action: str,
    ) -> GenerationRun:
        a = self._a

        async def work(tx: TxHandle) -> GenerationRun:
            run = await a.runs.get_run(tx, run_id, lock=True)
            if run is None:
                raise not_found()
            before_status, before_version = run.status, run.record_version
            after = copy.copy(run)
            apply(after)  # 409 WORKFLOW_STATE_CONFLICT where the state or guard does not fit
            new_version = await a.runs.save_gate(
                tx, after, expected_status=before_status, expected_record_version=before_version
            )
            if new_version is None:
                raise gate.conflict("The run changed while the action was being applied.")
            after.record_version = new_version
            await a.publish_progress(tx, after)
            return after

        async with a.obs.span(f"workflow.{action}"):
            after = await a.tx.run_in_tx(work)
        a.obs.gate_action(action)
        return after


class ApproveOutline(_Gate):
    """W-3 ``POST /generations/{id}/outline/approve`` ``{outline_version}``."""

    async def execute(
        self, identity: RequestIdentity, run_id: UUID, outline_version: int
    ) -> SignalAck:
        """Approve; stage 1 begins when the signal lands."""
        a = self._a
        run, _ = await a.load(run_id)
        await a.authorize(identity, run, eligibility=True)
        if gate.already_approved(run, outline_version):
            await a.signal(run, SIGNAL_APPROVE, {"outline_version": outline_version})
            return SignalAck.of(run)  # a lost signal is re-raised, not stranded
        after = await self._transition(
            run_id, lambda r: gate.approve(r, outline_version), action="approve"
        )
        await a.signal(after, SIGNAL_APPROVE, {"outline_version": outline_version})
        return SignalAck.of(after)


class ReviseOutline(_Gate):
    """W-4 ``POST /generations/{id}/outline`` ``{outline_version, sections}``."""

    async def execute(
        self, identity: RequestIdentity, run_id: UUID, edit: OutlineEdit
    ) -> SignalAck:
        """Record the revision (``outline_version`` + 1, here and nowhere else, RUN-7).

        The edited outline rides the signal as references and types. The section rows are the
        Worker Pool's (``rag_activity`` is their writer): the outline activity rewrites them.
        """
        a = self._a
        _validate_outline(edit)
        run, _ = await a.load(run_id)
        await a.authorize(identity, run, eligibility=True)
        if gate.already_revised(run, edit.outline_version):
            await a.signal(run, SIGNAL_REVISE, _revision(run.outline_version, edit))
            return SignalAck.of(run)  # a lost signal is re-raised, not stranded
        after = await self._transition(
            run_id, lambda r: gate.revise(r, edit.outline_version), action="revise"
        )
        await a.signal(after, SIGNAL_REVISE, _revision(after.outline_version, edit))
        return SignalAck.of(after)


def _revision(version: int | None, edit: OutlineEdit) -> dict[str, object]:
    return {
        "outline_version": version,
        "sections": [{"ref": s.ref, "type": s.type} for s in edit.sections],
    }


def _validate_outline(edit: OutlineEdit) -> None:
    refs = [s.ref for s in edit.sections]
    if (
        len(edit.sections) > MAX_OUTLINE_SECTIONS
        or len(set(refs)) != len(refs)
        or any(not s.ref or len(s.ref) > MAX_REF_CHARS or not s.type for s in edit.sections)
    ):
        raise DomainError(ErrorCode.WORKFLOW_STATE_CONFLICT, "The edited outline is not valid.")


class ResumeGeneration(_Gate):
    """W-7 ``POST /generations/{id}/resume`` and the quota-restored consumer's path (LD-70)."""

    async def execute(self, identity: RequestIdentity, run_id: UUID) -> SignalAck:
        """Resume a run suspended on quota."""
        a = self._a
        run, _ = await a.load(run_id)
        await a.authorize(identity, run, eligibility=True)
        after = await self._transition(run_id, gate.resume, action="resume")
        await a.signal(after, SIGNAL_RESUME)
        return SignalAck.of(after)


class CancelGeneration:
    """W-5 ``POST /generations/{id}/cancel`` ``{reason}``: scope only, never eligibility."""

    def __init__(self, actions: RunActions) -> None:
        self._a = actions

    async def execute(
        self, identity: RequestIdentity, run_id: UUID, reason: str | None
    ) -> SignalAck:
        """Run, artefact and in-flight sections close in one transaction, then the signal."""
        a = self._a
        reason = (reason or "").strip()[:MAX_REASON_CHARS]
        run, _ = await a.load(run_id)
        await a.authorize(identity, run, eligibility=False)  # a read-only tenant may still cancel
        if run.status is RunStatus.CANCELLED:
            await a.signal(run, SIGNAL_CANCEL, {"reason": reason})  # repeat: re-raise a lost signal
            return SignalAck.of(run)

        async def work(tx: TxHandle) -> GenerationRun:
            locked = await a.runs.get_run(tx, run_id, lock=True)
            if locked is None:
                raise not_found()
            before_status, before_version = locked.status, locked.record_version
            after = copy.copy(locked)
            gate.cancel(after)
            new_version = await a.runs.save_gate(
                tx, after, expected_status=before_status, expected_record_version=before_version
            )
            if new_version is None:
                raise gate.conflict("The run changed while it was being cancelled.")
            after.record_version = new_version
            artefact = await a.runs.get_artefact(tx, after.artefact_id, lock=True)
            if artefact is not None and artefact.status in (
                ArtefactStatus.PENDING,
                ArtefactStatus.GENERATING,
            ):
                artefact.move_to(ArtefactStatus.CANCELLED)  # ART-6: same transaction
                await a.runs.save_artefact_status(tx, artefact)
            # AS-8. No run_failure_reason means cancellation, so `internal` stands in for the
            # section's required reason: a cancel is not a failure of its sections.
            await a.runs.close_in_flight_sections(tx, after.artefact_id, RunFailureReason.INTERNAL)
            done, total = await a.runs.count_sections(tx, after.artefact_id)
            assert identity.principal_id is not None
            await a.audit(
                tx,
                audit.AuditEntryDraft(
                    tenant_id=after.tenant_id,
                    occurred_at=a.clock.now(),
                    entry_type=audit.GENERATION_CANCELLED[0],
                    action=audit.GENERATION_CANCELLED[1],
                    actor_type=AuditActorType.USER,
                    actor_id=identity.principal_id,
                    artefact_id=after.artefact_id,
                    source_event_type="GenerationCancelled",
                    source_event_id=f"GenerationCancelled:{after.id}",
                    target_type="artefact",
                    target_id=str(after.artefact_id),
                    metadata={"generation_run_id": str(after.id)},
                ),
            )
            await a.events.publish(
                tx,
                build_event(
                    after.tenant_id,
                    tender_subject(after.tender_id),
                    GenerationCancelled(
                        artefact_id=str(after.artefact_id),
                        generation_id=str(after.id),
                        principal_id=str(after.principal_id),
                        reason=reason,
                        sections_complete=done,
                        sections_total=total,
                        sections_durable=True,
                        tender_id=None if after.tender_id is None else str(after.tender_id),
                        recipients=recipients_of(after),
                    ),
                    correlation_id=str((after.run_input or {}).get("correlation_id") or ""),
                ),
            )
            return after

        async with a.obs.span("workflow.cancel"):
            after = await a.tx.run_in_tx(work)
        a.obs.gate_action("cancel")
        await a.signal(after, SIGNAL_CANCEL, {"reason": reason})
        return SignalAck.of(after)
