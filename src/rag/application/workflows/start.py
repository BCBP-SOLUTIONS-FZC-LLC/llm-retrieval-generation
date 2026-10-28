"""W-1 and W-6: admit, then start the run's workflow (LLD §5.7.12, §9.2, §13.3, ASYNC-2).

The shell, the run and its ``GenerationStarted`` row commit first; only then is the engine
called, under the run's deterministic ``workflow_id`` (RUN-2). A failed start never closes the
run: it stays ``pending`` and the orphan sweep starts it again, because the opposite order could
leave a workflow with no artefact row (GEN-3).
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from uuid import UUID

from fastapicommon.logging import get_logger

from rag.application.events.contracts import Recipient
from rag.application.generation.flow import (
    AdmissionRequest,
    AdmitGeneration,
    AsyncAdmitted,
    AsyncReplayed,
    request_hash,
)
from rag.application.retrieval.scope import ScopeSpec
from rag.application.workflows.observability import NullWorkflowObserver, WorkflowObserver
from rag.application.workflows.ports import (
    WORKFLOW_EXTRACTION,
    WORKFLOW_GENERATION,
    StartRequest,
    WorkflowClient,
)
from rag.domain.generation.eligibility import EligibilityVerdict
from rag.domain.generation.run import GenerationRun
from rag.domain.packs.profile import PackBundle
from rag.domain.shared.enums import (
    ArtefactKind,
    GenerationMode,
    GrantLevel,
    RunStage,
    RunStatus,
)
from rag.domain.shared.errors import DomainError, ErrorCode
from rag.domain.shared.identity import RequestIdentity
from rag.domain.workflows.ids import task_queue_for

log = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class QueueNames:
    """The three generation queues (WP-10)."""

    default: str = "llm-generation"
    enterprise: str = "llm-generation-enterprise"
    strict: str = "llm-generation-strict"

    def for_input(self, run_input: dict[str, object] | None) -> str:
        """The queue for the plan and residency a run's input carries."""
        plan = (run_input or {}).get("plan")
        residency = str((run_input or {}).get("residency_mode") or "standard")
        return task_queue_for(
            plan if isinstance(plan, str) else None,
            residency,
            default=self.default,
            enterprise=self.enterprise,
            strict=self.strict,
        )


@dataclass(frozen=True, slots=True)
class StartGenerationCommand:
    """W-1 after validation. ``idempotency_key`` is required (RUN-3)."""

    tender_id: UUID
    capability: str
    idempotency_key: str
    target_ref: str | None = None
    scope: ScopeSpec = field(default_factory=ScopeSpec)
    requested_mode: GenerationMode = GenerationMode.ASSISTANCE
    outline_gate: bool = True
    recipients: tuple[Recipient, ...] = ()
    correlation_id: str = ""


@dataclass(frozen=True, slots=True)
class StartExtractionCommand:
    """W-6 after validation: ``schema_ref`` in place of ``capability``."""

    tender_id: UUID
    schema_ref: str
    idempotency_key: str
    target_ref: str | None = None
    scope: ScopeSpec = field(default_factory=ScopeSpec)
    trigger_document_version_id: str | None = None
    recipients: tuple[Recipient, ...] = ()
    correlation_id: str = ""


@dataclass(frozen=True, slots=True)
class RunHandle:
    """The ``202`` / ``200`` body of W-1, W-6 and A-5: ids, status and the pinned pack versions."""

    generation_id: UUID
    artefact_id: UUID
    status: RunStatus
    eligibility: EligibilityVerdict
    domain_profile_version: str
    language_pack_version: str
    replayed: bool = False


def eligibility_of(run: GenerationRun) -> EligibilityVerdict:
    """The verdict recorded on the run at admission (RUN-1)."""
    return EligibilityVerdict(
        state=run.eligibility_state,
        requested_mode=run.requested_mode,
        effective_mode=run.effective_mode,
        clamped=run.clamped,
        clamp_reason=run.clamp_reason,
        profile_version=run.domain_profile_version,
    )


def handle_of(run: GenerationRun, *, replayed: bool) -> RunHandle:
    """A handle from a stored run."""
    return RunHandle(
        generation_id=run.id,
        artefact_id=run.artefact_id,
        status=run.status,
        eligibility=eligibility_of(run),
        domain_profile_version=run.domain_profile_version,
        language_pack_version=run.language_pack_version,
        replayed=replayed,
    )


class RunStarter:
    """Calls ``start_workflow`` for a committed run and maps failure to ``503`` (ASYNC-2)."""

    def __init__(
        self,
        workflows: WorkflowClient,
        queues: QueueNames,
        observer: WorkflowObserver | None = None,
    ) -> None:
        self._workflows, self._queues = workflows, queues
        self._obs = observer or NullWorkflowObserver()

    def require_engine(self) -> None:
        """Refuse before admission where no engine is configured.

        No ``pending`` row is made that nothing could ever start.
        """
        if not self._workflows.available:
            raise DomainError(
                ErrorCode.WORKFLOW_ENGINE_UNAVAILABLE, "No workflow engine is configured."
            )

    async def begin(self, run: GenerationRun, workflow_type: str) -> None:
        """Start under the run's own id and input; a repeat the engine absorbs is success."""
        if run.workflow_id is None or run.run_input is None:
            raise DomainError(
                ErrorCode.WORKFLOW_ENGINE_UNAVAILABLE, "The run has no workflow to start."
            )
        request = StartRequest(
            workflow_id=run.workflow_id,
            workflow_type=workflow_type,
            task_queue=self._queues.for_input(run.run_input),
            run_input=run.run_input,
        )
        try:
            outcome = await self._workflows.start(request)
        except DomainError:
            self._obs.workflow_start("failed")
            raise
        self._obs.workflow_start(outcome.value)


class _AsyncStart:
    """The shared W-1 / W-6 flow."""

    def __init__(
        self,
        admit: AdmitGeneration,
        starter: RunStarter,
        observer: WorkflowObserver | None = None,
    ) -> None:
        self._admit, self._starter = admit, starter
        self._obs = observer or NullWorkflowObserver()

    async def run(
        self, identity: RequestIdentity, request: AdmissionRequest, workflow_type: str
    ) -> RunHandle:
        self._starter.require_engine()
        async with self._obs.span("workflow.admit"):
            adm = await self._admit.execute_async(identity, request)
        replayed = isinstance(adm, AsyncReplayed)
        run = adm.run
        if replayed and run.status is not RunStatus.PENDING:
            return handle_of(run, replayed=True)  # the handle: the run is already past its start
        async with self._obs.span("workflow.start"):
            await self._starter.begin(run, workflow_type)  # a pending replay retries the start
        return handle_of(run, replayed=replayed)


class StartGeneration(_AsyncStart):
    """W-1 ``POST /api/v1/generations`` (§5.7.12)."""

    async def execute(self, identity: RequestIdentity, cmd: StartGenerationCommand) -> RunHandle:
        """``202`` handle for a new run, ``replayed`` for a repeat of the same key and body."""
        scope = replace(cmd.scope, tender_id=cmd.tender_id)  # the tender routes, never authorizes
        digest = request_hash(
            "generation",
            cmd.tender_id,
            cmd.capability,
            cmd.target_ref,
            sorted(map(str, scope.collection_ids)),
            sorted(scope.collection_types),
            cmd.requested_mode.value,
            cmd.outline_gate,
        )
        return await self.run(
            identity,
            AdmissionRequest(
                kind=ArtefactKind.DOCUMENT,
                capability=cmd.capability,
                section_type=cmd.capability,
                user_text="",
                scope=scope,
                required_level=GrantLevel.GENERATE,
                requested_mode=cmd.requested_mode,
                tender_id=cmd.tender_id,
                idempotency_key=cmd.idempotency_key,
                request_hash=digest,
                correlation_id=cmd.correlation_id,
                target_ref=cmd.target_ref,
                outline_gate=cmd.outline_gate,
                recipients=cmd.recipients,
            ),
            WORKFLOW_GENERATION,
        )


class StartAsyncExtraction(_AsyncStart):
    """W-6 ``POST /api/v1/extractions/async`` (§5.7.12): an extraction beyond R-4's budget."""

    async def execute(self, identity: RequestIdentity, cmd: StartExtractionCommand) -> RunHandle:
        """As W-1; ``422 PACK_INVALID`` where ``schema_ref`` is not the pack's."""

        def check(bundle: PackBundle) -> None:
            if cmd.schema_ref not in bundle.domain_profile.extraction_schemas:
                raise DomainError(
                    ErrorCode.PACK_INVALID,
                    f"schema_ref is not declared by pack {bundle.domain_profile.version}.",
                )

        scope = replace(cmd.scope, tender_id=cmd.tender_id)
        digest = request_hash(
            "extraction-async",
            cmd.tender_id,
            cmd.schema_ref,
            cmd.target_ref,
            cmd.trigger_document_version_id,
            sorted(map(str, scope.collection_ids)),
            sorted(scope.collection_types),
        )
        extras = {"schema_ref": cmd.schema_ref}
        if cmd.trigger_document_version_id is not None:
            extras["trigger_document_version_id"] = cmd.trigger_document_version_id
        return await self.run(
            identity,
            AdmissionRequest(
                kind=ArtefactKind.EXTRACTION,
                capability="extract",
                section_type=cmd.schema_ref,
                user_text="",
                scope=scope,
                required_level=GrantLevel.GENERATE,
                tender_id=cmd.tender_id,
                idempotency_key=cmd.idempotency_key,
                request_hash=digest,
                stage=RunStage.EXTRACT,
                correlation_id=cmd.correlation_id,
                check_pack=check,
                target_ref=cmd.target_ref,
                outline_gate=False,
                extras=extras,
                recipients=cmd.recipients,
            ),
            WORKFLOW_EXTRACTION,
        )


__all__ = [
    "AsyncAdmitted",
    "QueueNames",
    "RunHandle",
    "RunStarter",
    "StartAsyncExtraction",
    "StartExtractionCommand",
    "StartGeneration",
    "StartGenerationCommand",
    "handle_of",
]
