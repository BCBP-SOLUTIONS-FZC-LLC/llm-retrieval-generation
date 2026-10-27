"""Wiring of the synchronous generation routes R-2, R-3 and R-4 and the shared flow.

``GenerateServices.admit`` / ``lifecycle`` / ``complete`` / ``verify`` are what the workflows
slice reuses: the same admission, run-state and completion transaction, the same ladder.
"""

from __future__ import annotations

from dataclasses import dataclass

from fastapi import Request

from rag.application.generation.clock import SystemClock
from rag.application.generation.commands import AppendAuditEntry, MarkVersionsSuperseded
from rag.application.generation.extract import ExtractStructured
from rag.application.generation.flow import (
    AdmitGeneration,
    CompleteGeneration,
    GenerationSettings,
    RunLifecycle,
)
from rag.application.generation.generate import (
    GenerateAnswer,
    GenerateSectionDraft,
    ShortFormEngine,
)
from rag.application.generation.ports import ArtefactContentStore, UserContextProvider
from rag.application.shared.events import EventPublisher
from rag.application.shared.ports import TransactionRunner
from rag.application.verification.verify import VerificationConfig, VerifySection
from rag.domain.verification.scoring import TierThresholds
from rag.domain.verification.tiers import LadderThresholds
from rag.infrastructure.config import Settings
from rag.infrastructure.events.publisher import OutboxEventPublisher
from rag.infrastructure.iam.llm_context import HttpUserContext
from rag.infrastructure.observability.generation_metrics import build_generation_observer
from rag.infrastructure.persistence.generation.probes import CorpusTenderGate, PgStoreProbe
from rag.infrastructure.persistence.generation.repositories import (
    PgArtefactReadRepository,
    PgAuditChainRepository,
    PgVersionRepository,
)
from rag.infrastructure.persistence.generation.writes import PgGenerationWriteRepository
from rag.infrastructure.persistence.pools import ContextTenantBinder
from rag.presentation.retrieval_dependencies import RetrievalServices
from rag.presentation.scope_dependencies import ScopeServices


@dataclass(frozen=True, slots=True)
class GenerateServices:
    """R-2, R-3, R-4 and the building blocks the workflows slice reuses."""

    answer: GenerateAnswer
    draft: GenerateSectionDraft
    extract: ExtractStructured
    admit: AdmitGeneration
    lifecycle: RunLifecycle
    complete: CompleteGeneration
    verify: VerifySection
    settings: GenerationSettings
    engine: ShortFormEngine

    async def drain(self, timeout: float) -> None:
        """Let every detached producer and extraction finish before shutdown closes the clients."""
        await self.engine.drain(timeout)
        await self.extract.drain(timeout)


def generation_settings(settings: Settings) -> GenerationSettings:
    """``GenerationSettings`` from the service settings."""
    return GenerationSettings(
        window_tokens=settings.generation_context_window_tokens,
        output_fraction=settings.context_output_reserve_fraction,
        user_input_max_fraction=settings.context_user_input_max_fraction,
        idempotency_ttl_seconds=settings.idempotency_ttl_seconds,
        heartbeat_seconds=float(settings.sse_heartbeat_seconds),
        answer_deadline_seconds=settings.generation_answer_deadline_seconds,
        draft_deadline_seconds=settings.generation_draft_deadline_seconds,
        extraction_deadline_seconds=float(settings.extraction_sync_timeout_seconds),
        parser_version=settings.pipeline_parser_version,
        chunker_version=settings.pipeline_chunker_version,
    )


def build_generate_services(
    settings: Settings,
    generation_tx: TransactionRunner,
    corpus_tx: TransactionRunner,
    retrieval: RetrievalServices,
    scope: ScopeServices,
    store: ArtefactContentStore,
    *,
    events: EventPublisher | None = None,
    user_context: UserContextProvider | None = None,
) -> GenerateServices:
    """Construct R-2, R-3 and R-4 over the retrieval pipeline and the generation pool."""
    cfg = generation_settings(settings)
    observer = build_generation_observer(settings)
    events = events or OutboxEventPublisher()
    repo, clock = PgGenerationWriteRepository(), SystemClock()
    audit = AppendAuditEntry(PgAuditChainRepository())
    binder, reads = ContextTenantBinder(), PgArtefactReadRepository()
    if user_context is None and settings.iam_base_url:
        user_context = HttpUserContext(settings.iam_base_url, scope.cache)
    admit = AdmitGeneration(
        generation_tx,
        repo,
        scope.adapter,
        retrieval.packs,
        retrieval.tokens,
        PgStoreProbe(generation_tx),
        CorpusTenderGate(corpus_tx),
        events,
        clock,
        cfg,
    )
    lifecycle = RunLifecycle(generation_tx, repo, audit, clock)
    complete = CompleteGeneration(
        generation_tx,
        repo,
        store,
        audit,
        events,
        clock,
        MarkVersionsSuperseded(PgVersionRepository()),
    )
    verify = VerifySection(
        VerificationConfig(
            ladder=LadderThresholds(
                span_overlap=settings.verify_span_overlap,
                entail_supported=settings.verify_entail_supported,
                entail_unsupported=settings.verify_entail_unsupported,
                tier0_disagreement=settings.verify_tier0_disagreement,
            ),
            tiers=TierThresholds(
                settings.groundedness_partial_threshold, settings.groundedness_supported_threshold
            ),
            completeness_partial=settings.completeness_partial_threshold,
            tier0_sample_rate=settings.verify_tier0_sample_rate,
        ),
        observer,
    )
    engine = ShortFormEngine(
        admit=admit,
        lifecycle=lifecycle,
        complete=complete,
        pipeline=retrieval.pipeline,
        recorder=retrieval.recorder,
        gateway=retrieval.gateway,
        verify=verify,
        reads=reads,
        tx=generation_tx,
        binder=binder,
        settings=cfg,
        user_context=user_context,
        observer=observer,
        tokens=retrieval.tokens,
    )
    return GenerateServices(
        answer=GenerateAnswer(engine, cfg),
        draft=GenerateSectionDraft(engine, cfg),
        extract=ExtractStructured(
            admit=admit,
            lifecycle=lifecycle,
            complete=complete,
            pipeline=retrieval.pipeline,
            recorder=retrieval.recorder,
            gateway=retrieval.gateway,
            reads=reads,
            tx=generation_tx,
            store=store,
            binder=binder,
            settings=cfg,
            observer=observer,
        ),
        admit=admit,
        lifecycle=lifecycle,
        complete=complete,
        verify=verify,
        settings=cfg,
        engine=engine,
    )


def get_generate(request: Request) -> GenerateServices:
    """The app's R-2/R-3/R-4 services (tests override this dependency)."""
    services: GenerateServices = request.app.state.container.generate
    return services
