"""Outbound ports of the workflows module (LLD §9): the Temporal client and the IAM re-read.

This service's whole use of the Workflow Engine is two calls, ``start_workflow`` and
``signal_workflow`` (LD-88). It never queries a workflow (ASYNC-5), so there is no ``describe``.
The workflow and signal names are not in the LLD; they are this service's choice and are listed
in ``docs/worker-pool-contract.md`` as the contract the Worker Pool must implement.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Protocol
from uuid import UUID

from rag.domain.shared.identity import RequestIdentity

WORKFLOW_GENERATION = "GenerationWorkflow"
WORKFLOW_EXTRACTION = "ExtractionWorkflow"

SIGNAL_APPROVE = "approve_outline"
SIGNAL_REVISE = "revise_outline"
SIGNAL_CANCEL = "cancel"
SIGNAL_RESUME = "resume"


class StartOutcome(StrEnum):
    """``already_started`` is success for W-1, W-6, A-5 and the sweep (ASYNC-2), 409 for W-8."""

    STARTED = "started"
    ALREADY_STARTED = "already_started"


@dataclass(frozen=True, slots=True)
class StartRequest:
    """One ``start_workflow``. ``restart`` is W-8's.

    A closed workflow's id may be reused, a live one is refused (``already_started``); otherwise a
    finished id is never reused.
    """

    workflow_id: str
    workflow_type: str
    task_queue: str
    run_input: dict[str, Any] = field(default_factory=dict)
    restart: bool = False


class WorkflowClient(Protocol):
    """The Temporal client. Every failure to reach the engine is ``WORKFLOW_ENGINE_UNAVAILABLE``."""

    @property
    def available(self) -> bool:
        """``False`` while no engine is configured: nothing may be reported as started."""
        ...

    async def start(self, request: StartRequest) -> StartOutcome:
        """Start under ``request.workflow_id`` (2 s, 2 retries with backoff, §14.3)."""
        ...

    async def signal(
        self, workflow_id: str, signal: str, payload: dict[str, Any] | None = None
    ) -> None:
        """Signal the run's workflow.

        Raises ``DomainError(WORKFLOW_ENGINE_UNAVAILABLE)`` where the engine is unreachable or no
        workflow answers to ``workflow_id``.
        """
        ...


class PrincipalDirectory(Protocol):
    """ASYNC-11: re-read a stored principal's roles and departments from IAM.

    The quota-restored consumer has no request, so nothing else can feed scope re-resolution.
    Never answer with an empty identity: an empty department list would falsely end runs
    ``scope_revoked``. Raises ``DomainError(SCOPE_STORE_UNAVAILABLE)`` where IAM cannot answer.
    """

    async def identity_for(
        self,
        tenant_id: UUID,
        principal_id: UUID,
        *,
        plan: str | None,
        residency_mode: str,
    ) -> RequestIdentity:
        """The principal's current identity inside ``tenant_id``."""
        ...
