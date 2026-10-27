"""The two ports the events slice defined for the workflow client, over the workflows use cases.

``WorkflowStarter`` is the orphan-artefact sweep's (ASYNC-2, RUN-8): start again under the run's
own id and stored input; the engine absorbs a run that did start. ``RunResumer`` is the
quota-restored consumer's (LD-70, ASYNC-8): the W-7 path for a run suspended on quota, for a
principal who has no request, so the principal is re-read from IAM (ASYNC-11).
"""

from __future__ import annotations

import logging
from typing import Any

from rag.application.events.ports import SuspendedRun
from rag.application.shared.ports import TransactionRunner, TxHandle
from rag.application.workflows.ports import (
    WORKFLOW_EXTRACTION,
    WORKFLOW_GENERATION,
    PrincipalDirectory,
    StartRequest,
    WorkflowClient,
)
from rag.application.workflows.signals import ResumeGeneration
from rag.application.workflows.start import QueueNames
from rag.domain.shared.errors import DomainError, ErrorCode
from rag.domain.workflows import gate
from rag.domain.workflows.repositories import RunRepository

log = logging.getLogger(__name__)
SKIPPED = frozenset(
    {
        ErrorCode.WORKFLOW_STATE_CONFLICT,  # no longer suspended: someone else resumed it
        ErrorCode.ARTEFACT_NOT_FOUND,  # scope empty: the run ended scope_revoked (ASYNC-15)
        ErrorCode.GENERATION_NOT_PERMITTED,  # ineligible now
        ErrorCode.GRANT_LEVEL_INSUFFICIENT,  # the principal's grant dropped: a person decides
    }
)
"""Verdicts about one run that must not poison the batch. Outages are not here: they raise."""


def workflow_type_for(run_input: dict[str, Any] | None) -> str:
    """``ExtractionWorkflow`` for a W-6 run, ``GenerationWorkflow`` otherwise."""
    return (
        WORKFLOW_EXTRACTION
        if (run_input or {}).get("kind") == "extraction"
        else WORKFLOW_GENERATION
    )


class OrphanRestarter:
    """``WorkflowStarter``: re-attempt a start the shell committed but the engine never saw."""

    def __init__(self, workflows: WorkflowClient, queues: QueueNames) -> None:
        self._workflows, self._queues = workflows, queues

    @property
    def available(self) -> bool:
        """``False`` while no engine is configured, so the sweep does nothing (no false verdict)."""
        return self._workflows.available

    async def start(self, workflow_id: str | None, run_input: dict[str, object] | None) -> None:
        """Start once; ``already_started`` is success.

        A run with no workflow raises, so the sweep fails it (a synchronous run has no workflow to
        recover).
        """
        if workflow_id is None or not run_input or run_input.get("synchronous"):
            raise DomainError(
                ErrorCode.WORKFLOW_STATE_CONFLICT, "The run has no workflow to start."
            )
        await self._workflows.start(
            StartRequest(
                workflow_id=workflow_id,
                workflow_type=workflow_type_for(run_input),
                task_queue=self._queues.for_input(run_input),
                run_input=dict(run_input),
            )
        )


class QuotaRunResumer:
    """``RunResumer``: W-7 for one quota-suspended run, as its stored principal (ASYNC-8)."""

    def __init__(
        self,
        tx: TransactionRunner,
        runs: RunRepository,
        resume: ResumeGeneration,
        directory: PrincipalDirectory,
        workflows: WorkflowClient,
    ) -> None:
        self._tx, self._runs, self._resume = tx, runs, resume
        self._directory, self._workflows = directory, workflows

    @property
    def available(self) -> bool:
        """``False`` while no engine is configured: nothing may be ledgered as resumed."""
        return self._workflows.available

    async def resume(self, run: SuspendedRun) -> None:
        """Re-resolve scope and eligibility for the stored principal, then signal.

        Only a run still ``suspended`` on ``quota_exhausted`` is signalled (ASYNC-8). A verdict
        about this run (moved on, scope empty, ineligible) is logged and skipped; an outage
        raises so the message is redelivered.
        """

        async def read(tx: TxHandle) -> Any:
            return await self._runs.get_run(tx, run.run_id)

        stored = await self._tx.run_in_tx(read, read_only=True)
        if stored is None or not gate.resumable_on_quota(stored):
            return
        identity = await self._directory.identity_for(
            stored.tenant_id,
            stored.principal_id,
            plan=(stored.run_input or {}).get("plan"),
            residency_mode=str((stored.run_input or {}).get("residency_mode") or "standard"),
        )
        try:
            await self._resume.execute(identity, stored.id)
        except DomainError as exc:
            if exc.code not in SKIPPED:
                raise
            log.info("quota_resume_skipped code=%s", exc.code.value)
