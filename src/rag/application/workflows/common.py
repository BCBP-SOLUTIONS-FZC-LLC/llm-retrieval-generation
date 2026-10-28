"""What W-3, W-4, W-5, W-7 and W-8 share.

Loading, scope re-resolution (API-10, ASYNC-4), ending a run ``scope_revoked`` (ASYNC-15), the
progress and failure events, and the signal.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from fastapicommon.logging import get_logger

from rag.application.events.contracts import (
    GenerationCancelled,
    GenerationFailed,
    GenerationProgressed,
    Recipient,
    build_event,
    tender_subject,
)
from rag.application.generation.commands import AppendAuditEntry
from rag.application.packs.ports import PackResolver
from rag.application.packs.resolve import resolve_or_default
from rag.application.shared.events import EventPublisher
from rag.application.shared.ports import TransactionRunner, TxHandle
from rag.application.shared.scope import ScopeAuthorizer, ScopeTarget
from rag.application.workflows.observability import NullWorkflowObserver, WorkflowObserver
from rag.application.workflows.ports import SIGNAL_CANCEL, WorkflowClient
from rag.domain.generation import audit
from rag.domain.generation.artefact import Artefact
from rag.domain.generation.eligibility import resolve_eligibility
from rag.domain.generation.repositories import Clock
from rag.domain.generation.run import GenerationRun
from rag.domain.shared.enums import (
    ArtefactStatus,
    AuditActorType,
    GrantLevel,
    OutlineState,
    RunFailureReason,
    RunStatus,
)
from rag.domain.shared.errors import DomainError, ErrorCode
from rag.domain.shared.identity import RequestIdentity
from rag.domain.workflows.repositories import RunRepository

log = get_logger(__name__)


def not_found() -> DomainError:
    """``404 ARTEFACT_NOT_FOUND``: absent or out of scope, indistinguishable (API-10, LD-62)."""
    return DomainError(ErrorCode.ARTEFACT_NOT_FOUND, "The generation was not found.")


@dataclass(frozen=True, slots=True)
class SignalAck:
    """The ``202`` body of W-3, W-4, W-5, W-7: the resulting workflow status."""

    generation_id: UUID
    artefact_id: UUID
    status: RunStatus
    outline_state: OutlineState
    outline_version: int | None

    @staticmethod
    def of(run: GenerationRun) -> SignalAck:
        """From the run after the transition."""
        return SignalAck(
            run.id, run.artefact_id, run.status, run.outline_state, run.outline_version
        )


def recipients_of(run: GenerationRun) -> list[Recipient]:
    """The recipients the starting request named, read from the run's input (LD-83)."""
    raw = (run.run_input or {}).get("recipients") or []
    return [Recipient(str(r["kind"]), str(r["id"])) for r in raw if isinstance(r, dict)]


class RunActions:
    """Collaborators and helpers of the gate, cancel, resume and rematerialise use cases."""

    def __init__(
        self,
        *,
        tx: TransactionRunner,
        runs: RunRepository,
        scope: ScopeAuthorizer,
        packs: PackResolver,
        workflows: WorkflowClient,
        events: EventPublisher,
        audit_log: AppendAuditEntry,
        clock: Clock,
        observer: WorkflowObserver | None = None,
    ) -> None:
        self.tx, self.runs, self.scope, self.packs = tx, runs, scope, packs
        self.workflows, self.events, self.audit, self.clock = workflows, events, audit_log, clock
        self.obs = observer or NullWorkflowObserver()

    # --- load and authorize -------------------------------------------------------------------
    async def load(self, run_id: UUID) -> tuple[GenerationRun, Artefact]:
        """The run and its artefact (RLS-scoped), or ``404``."""

        async def read(tx: TxHandle) -> tuple[GenerationRun, Artefact] | None:
            run = await self.runs.get_run(tx, run_id)
            if run is None:
                return None
            artefact = await self.runs.get_artefact(tx, run.artefact_id)
            return None if artefact is None else (run, artefact)

        found = await self.tx.run_in_tx(read, read_only=True)
        if found is None:
            raise not_found()
        return found

    async def authorize(
        self, identity: RequestIdentity, run: GenerationRun, *, eligibility: bool
    ) -> None:
        """Re-resolve scope (and, for W-3, W-4, W-7, eligibility) before acting (ASYNC-4).

        An empty scope ends the run ``scope_revoked`` and answers ``404`` (ASYNC-15, LD-62).
        """
        try:
            await self.scope.require_level(
                identity, ScopeTarget(tender_id=run.tender_id), GrantLevel.GENERATE
            )
        except DomainError as exc:
            if exc.code is ErrorCode.COLLECTION_NOT_FOUND:
                await self.revoke_scope(identity.principal_id, run.id)
                raise not_found() from exc
            raise
        if eligibility:
            artefact = (await self.load(run.id))[1]
            bundle, _ = await resolve_or_default(self.packs, identity.tenant_id)
            resolve_eligibility(
                bundle.domain_profile.eligibility_for(artefact.capability),
                run.requested_mode,
                bundle.domain_profile.version,
            )  # raises 403 GENERATION_NOT_PERMITTED

    # --- progress events ----------------------------------------------------------------------
    async def publish_progress(
        self, tx: TxHandle, run: GenerationRun, artefact: Artefact | None = None
    ) -> None:
        """``GenerationProgressed``.

        ``progress_seq`` is the run's trigger-bumped ``record_version``, assigned by the very write
        that changed the state (EVT-13).
        """
        done, total = await self.runs.count_sections(tx, run.artefact_id)
        await self.events.publish(
            tx,
            build_event(
                run.tenant_id,
                tender_subject(run.tender_id),
                GenerationProgressed(
                    artefact_id=str(run.artefact_id),
                    generation_id=str(run.id),
                    principal_id=str(run.principal_id),
                    progress_seq=run.record_version,
                    run_status=run.status,
                    sections_complete=done,
                    sections_total=total,
                    outline_state=run.outline_state,
                    observed_at=self.clock.now().isoformat().replace("+00:00", "Z"),
                    tender_id=None if run.tender_id is None else str(run.tender_id),
                    stage=run.current_stage,
                    recipients=recipients_of(run),
                ),
                correlation_id=str((run.run_input or {}).get("correlation_id") or ""),
            ),
        )

    # --- ending a run -------------------------------------------------------------------------
    async def revoke_scope(self, actor: UUID | None, run_id: UUID) -> None:
        """ASYNC-15 / LD-62: end the run ``scope_revoked`` in one transaction.

        Run ``failed``, artefact ``failed`` where it was live, in-flight sections closed (AS-8),
        the audit entry and ``GenerationFailed``.


        A repeat finds a terminal run and does nothing. The workflow is then told to stop, best
        effort: the signal is not what makes the run end, the row is.
        """

        async def work(tx: TxHandle) -> GenerationRun | None:
            run = await self.runs.get_run(tx, run_id, lock=True)
            if run is None or run.is_terminal:
                return None
            before_status, before_version = run.status, run.record_version
            after = copy.copy(run)
            after.revoke_scope()
            new_version = await self.runs.save_gate(
                tx, after, expected_status=before_status, expected_record_version=before_version
            )
            if new_version is None:
                return None  # another writer moved it first; its outcome stands
            after.record_version = new_version
            artefact = await self.runs.get_artefact(tx, after.artefact_id, lock=True)
            if artefact is not None and artefact.status in (
                ArtefactStatus.PENDING,
                ArtefactStatus.GENERATING,
            ):
                artefact.move_to(ArtefactStatus.FAILED)
                await self.runs.save_artefact_status(tx, artefact)
            await self.runs.close_in_flight_sections(
                tx, after.artefact_id, RunFailureReason.SCOPE_REVOKED
            )
            done, total = await self.runs.count_sections(tx, after.artefact_id)
            await self.audit(
                tx,
                audit.AuditEntryDraft(
                    tenant_id=after.tenant_id,
                    occurred_at=self.clock.now(),
                    entry_type=audit.GENERATION_FAILED[0],
                    action=audit.GENERATION_FAILED[1],
                    actor_type=AuditActorType.USER if actor else AuditActorType.SERVICE_ACCOUNT,
                    actor_id=actor or after.principal_id,
                    artefact_id=after.artefact_id,
                    source_event_type="GenerationFailed",
                    source_event_id=f"GenerationFailed:{after.id}",
                    target_type="artefact",
                    target_id=str(after.artefact_id),
                    metadata={
                        "generation_run_id": str(after.id),
                        "failure_reason": RunFailureReason.SCOPE_REVOKED.value,
                        "failed_at_stage": None
                        if after.failed_at_stage is None
                        else after.failed_at_stage.value,
                    },
                ),
            )
            await self.events.publish(
                tx,
                build_event(
                    after.tenant_id,
                    tender_subject(after.tender_id),
                    GenerationFailed(
                        artefact_id=str(after.artefact_id),
                        principal_id=str(after.principal_id),
                        reason=RunFailureReason.SCOPE_REVOKED,
                        sections_complete=done,
                        sections_total=total,
                        sections_durable=True,
                        generation_id=str(after.id),
                        tender_id=None if after.tender_id is None else str(after.tender_id),
                        failed_at_stage=after.failed_at_stage,
                        recipients=recipients_of(after),
                    ),
                    correlation_id=str((after.run_input or {}).get("correlation_id") or ""),
                ),
            )
            return after

        ended = await self.tx.run_in_tx(work)
        if ended is None:
            return
        self.obs.scope_revoked()
        if ended.workflow_id is not None:
            try:
                await self.workflows.signal(
                    ended.workflow_id, SIGNAL_CANCEL, {"reason": "scope_revoked"}
                )
            except DomainError:
                log.warning("scope_revoked_signal_failed")  # the row already ended the run

    # --- signalling ---------------------------------------------------------------------------
    async def signal(
        self, run: GenerationRun, signal: str, payload: dict[str, Any] | None = None
    ) -> None:
        """The signal, after the row committed (LD-88). ``503`` where it cannot be raised."""
        if run.workflow_id is None:
            raise DomainError(
                ErrorCode.WORKFLOW_STATE_CONFLICT, "A synchronous run has no workflow to signal."
            )
        try:
            await self.workflows.signal(run.workflow_id, signal, payload)
        except DomainError:
            self.obs.workflow_signal(signal, "failed")
            raise
        self.obs.workflow_signal(signal, "sent")


__all__ = ["GenerationCancelled", "RunActions", "SignalAck", "not_found", "recipients_of"]
