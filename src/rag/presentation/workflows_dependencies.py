"""Wiring of the workflows module and the regeneration use cases (W-1 to W-8, A-5 to A-7).

``WorkflowServices.restarter`` and ``resumer`` replace the events slice's ``Unwired*`` stand-ins:
the orphan sweep and the quota consumer now start and signal for real where ``TEMPORAL_ADDRESS``
is set, and stay fail-closed where it is not.
"""

from __future__ import annotations

from dataclasses import dataclass

from fastapi import Request

from rag.application.generation.clock import SystemClock
from rag.application.generation.commands import AppendAuditEntry
from rag.application.generation.ports import ArtefactContentStore, TenderGate
from rag.application.generation.refiner import InstructionRefiner
from rag.application.generation.regenerate import (
    RegenerateDocument,
    RegenerateSection,
    RegenerationDeps,
    ResolveFactConflict,
)
from rag.application.shared.ports import TransactionRunner
from rag.application.shared.scope import ScopeAuthorizer
from rag.application.workflows.bridges import OrphanRestarter, QuotaRunResumer
from rag.application.workflows.common import RunActions
from rag.application.workflows.operator import RematerialiseRun
from rag.application.workflows.ports import PrincipalDirectory, WorkflowClient
from rag.application.workflows.signals import (
    ApproveOutline,
    CancelGeneration,
    ResumeGeneration,
    ReviseOutline,
)
from rag.application.workflows.start import (
    RunStarter,
    StartAsyncExtraction,
    StartGeneration,
)
from rag.application.workflows.state import GetGenerationState
from rag.infrastructure.config import Settings
from rag.infrastructure.events.publisher import OutboxEventPublisher
from rag.infrastructure.iam.principals import HttpPrincipalDirectory, UnavailablePrincipalDirectory
from rag.infrastructure.observability.workflow_metrics import build_workflow_observer
from rag.infrastructure.persistence.generation.regeneration import (
    PgFactRepository,
    PgInstructionRefinementRepository,
    PgRegenerationRepository,
)
from rag.infrastructure.persistence.generation.repositories import (
    PgArtefactReadRepository,
    PgAuditChainRepository,
)
from rag.infrastructure.persistence.generation.workflows import PgRunRepository
from rag.infrastructure.persistence.generation.writes import PgGenerationWriteRepository
from rag.infrastructure.temporal.factory import build_workflow_client, queue_names
from rag.presentation.generate_dependencies import GenerateServices
from rag.presentation.retrieval_dependencies import RetrievalServices


@dataclass(frozen=True, slots=True)
class WorkflowServices:
    """Use cases of the workflows and regeneration slice, and the two consumer-facing bridges."""

    client: WorkflowClient
    start: StartGeneration
    start_extraction: StartAsyncExtraction
    state: GetGenerationState
    approve: ApproveOutline
    revise: ReviseOutline
    cancel: CancelGeneration
    resume: ResumeGeneration
    rematerialise: RematerialiseRun
    regenerate_document: RegenerateDocument
    regenerate_section: RegenerateSection
    resolve_conflict: ResolveFactConflict
    restarter: OrphanRestarter
    resumer: QuotaRunResumer


def build_workflow_services(
    settings: Settings,
    generation_tx: TransactionRunner,
    generate: GenerateServices,
    retrieval: RetrievalServices,
    scope: ScopeAuthorizer,
    store: ArtefactContentStore,
    gate: TenderGate,
    *,
    client: WorkflowClient | None = None,
    directory: PrincipalDirectory | None = None,
) -> WorkflowServices:
    """Construct the module over the generation pool and the shared generation building blocks."""
    client = client or build_workflow_client(settings)
    queues, clock = queue_names(settings), SystemClock()
    observer = build_workflow_observer(settings)
    events = OutboxEventPublisher()
    audit = AppendAuditEntry(PgAuditChainRepository())
    runs, write = PgRunRepository(), PgGenerationWriteRepository()
    starter = RunStarter(client, queues, observer)
    actions = RunActions(
        tx=generation_tx,
        runs=runs,
        scope=scope,
        packs=retrieval.packs,
        workflows=client,
        events=events,
        audit_log=audit,
        clock=clock,
        observer=observer,
    )
    resume = ResumeGeneration(actions)
    directory = directory or (
        HttpPrincipalDirectory(settings.iam_base_url)
        if settings.iam_base_url
        else UnavailablePrincipalDirectory()
    )
    deps = RegenerationDeps(
        tx=generation_tx,
        reads=PgArtefactReadRepository(),
        regen=PgRegenerationRepository(),
        scope=scope,
        clock=clock,
        observer=observer,
    )
    facts, refinements = PgFactRepository(), PgInstructionRefinementRepository()
    refiner = InstructionRefiner(
        retrieval.gateway,
        enabled=settings.refiner_instruction_enabled,
        deadline_seconds=settings.refiner_instruction_deadline_ms / 1000,
        observer=retrieval.observer,
    )
    return WorkflowServices(
        client=client,
        start=StartGeneration(generate.admit, starter, observer),
        start_extraction=StartAsyncExtraction(generate.admit, starter, observer),
        state=GetGenerationState(generation_tx, runs, scope),
        approve=ApproveOutline(actions),
        revise=ReviseOutline(actions),
        cancel=CancelGeneration(actions),
        resume=resume,
        rematerialise=RematerialiseRun(generation_tx, runs, client, queues, audit, clock, observer),
        regenerate_document=RegenerateDocument(
            deps,
            generate.admit,
            write,
            runs.find_run_by_workflow_id,
            runs.lock_workflow_id,
            events,
            starter,
        ),
        regenerate_section=RegenerateSection(
            deps,
            generate.admit,
            generate.engine,
            generate.lifecycle,
            write,
            facts,
            refinements,
            refiner,
            store,
            events,
            generate.settings,
        ),
        resolve_conflict=ResolveFactConflict(deps, facts, refinements, audit, gate),
        restarter=OrphanRestarter(client, queues),
        resumer=QuotaRunResumer(generation_tx, runs, resume, directory, client),
    )


def get_workflows(request: Request) -> WorkflowServices:
    """The app's workflow services (tests override this dependency)."""
    services: WorkflowServices = request.app.state.container.workflows
    return services
