"""W-8 ``POST /api/v1/operator/generations/{id}/rematerialise`` (LLD §5.7.14, §13.11, §18.9).

Restarts a run whose Temporal workflow was lost, from the run row, under the same
``workflow_id`` and from ``run_input`` (RUN-8). Operator-only (``platform_operator``,
re-checked here, API-14) and audited as ``cross_tenant_access`` with the ticket reference.
It relaxes nothing in ASYNC-8: that forbids an unauthorized or automatic transition, and this
is neither.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID, uuid4

from rag.application.generation.commands import AppendAuditEntry
from rag.application.shared.ports import TransactionRunner, TxHandle
from rag.application.workflows.bridges import workflow_type_for
from rag.application.workflows.common import not_found
from rag.application.workflows.observability import NullWorkflowObserver, WorkflowObserver
from rag.application.workflows.ports import StartOutcome, StartRequest, WorkflowClient
from rag.application.workflows.start import QueueNames
from rag.domain.generation import audit
from rag.domain.generation.repositories import Clock
from rag.domain.shared.enums import AuditActorType
from rag.domain.shared.errors import DomainError, ErrorCode
from rag.domain.shared.identity import RequestIdentity
from rag.domain.workflows import gate
from rag.domain.workflows.repositories import RunRepository

log = logging.getLogger(__name__)
OPERATOR_ROLE = "platform_operator"
TICKET_MAX_CHARS = 128
REASON_MAX_CHARS = 512


@dataclass(frozen=True, slots=True)
class Rematerialised:
    """W-8's ``202`` body."""

    generation_id: UUID
    artefact_id: UUID
    workflow_id: str
    rematerialised_at: datetime


class RematerialiseRun:
    """W-8."""

    def __init__(
        self,
        tx: TransactionRunner,
        runs: RunRepository,
        workflows: WorkflowClient,
        queues: QueueNames,
        audit_log: AppendAuditEntry,
        clock: Clock,
        observer: WorkflowObserver | None = None,
    ) -> None:
        self._tx, self._runs, self._workflows, self._queues = tx, runs, workflows, queues
        self._audit, self._clock = audit_log, clock
        self._obs = observer or NullWorkflowObserver()

    async def execute(
        self, identity: RequestIdentity, run_id: UUID, ticket_ref: str, reason: str | None
    ) -> Rematerialised:
        """Restart the run's workflow; ``409`` where it is live or the run is not restartable."""
        if not identity.has_role(OPERATOR_ROLE):
            raise DomainError(ErrorCode.OPERATOR_REQUIRED, "platform_operator is not asserted.")
        ticket = (ticket_ref or "").strip()
        if not ticket or len(ticket) > TICKET_MAX_CHARS:
            raise ValueError("ticket_ref is required (the route validates it)")
        run = await self._tx.run_in_tx(lambda tx: self._runs.get_run(tx, run_id), read_only=True)
        if run is None:
            raise not_found()
        gate.require_restartable(run)
        if run.workflow_id is None or run.run_input is None:
            raise gate.conflict("The run has no workflow to restart.")
        if not self._workflows.available:
            raise DomainError(ErrorCode.WORKFLOW_ENGINE_UNAVAILABLE, "No engine is configured.")
        outcome = await self._workflows.start(
            StartRequest(
                workflow_id=run.workflow_id,
                workflow_type=workflow_type_for(run.run_input),
                task_queue=self._queues.for_input(run.run_input),
                run_input=run.run_input,
                restart=True,
            )
        )
        if outcome is StartOutcome.ALREADY_STARTED:  # a live workflow: never two for one run
            self._obs.workflow_start("already_started")
            raise gate.conflict("The engine reports a live workflow for this run.")
        self._obs.workflow_start("started")
        now = self._clock.now()
        assert identity.principal_id is not None
        reason = (reason or "").strip()[:REASON_MAX_CHARS]

        async def record(tx: TxHandle) -> None:
            await self._audit(
                tx,
                audit.AuditEntryDraft(
                    tenant_id=run.tenant_id,
                    occurred_at=now,
                    entry_type=audit.CROSS_TENANT_ACCESS[0],
                    action=audit.CROSS_TENANT_ACCESS[1],
                    actor_type=AuditActorType.USER,
                    actor_id=identity.principal_id,
                    artefact_id=run.artefact_id,
                    source_event_type="cross_tenant_access",
                    source_event_id=f"cross_tenant_access:{run.id}:{uuid4()}",
                    target_type="generation_run",
                    target_id=str(run.id),
                    metadata={
                        "operation": "rematerialise",
                        "artefact_id": str(run.artefact_id),
                        "ticket_ref": ticket,
                        "reason": reason,
                        "tenants_read": 1,
                    },
                ),
            )

        try:
            await self._tx.run_in_tx(record)
        except Exception:
            # The workflow already started: refusing now would invite a second start. The access
            # is the control (§15.3), so a failed record is loud, never silent.
            log.exception("cross_tenant_access_not_recorded ticket_ref=%s", ticket)
            raise
        self._obs.gate_action("rematerialise")
        log.warning("cross_tenant_access operation=rematerialise ticket_ref=%s", ticket)
        return Rematerialised(run.id, run.artefact_id, run.workflow_id, now)
