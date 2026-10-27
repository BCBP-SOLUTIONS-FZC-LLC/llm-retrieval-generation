"""Wiring of the retrieval module: pipeline, use cases, and the Gateway / Tender adapters.

``GatewayServices`` is the shared piece: the generation slice takes ``gateway`` from it.
"""

from __future__ import annotations

from dataclasses import dataclass

from fastapi import Request

from rag.application.packs.ports import PackResolver
from rag.application.retrieval.classify import ProposeClassification
from rag.application.retrieval.pipeline import PipelineConfig, RetrievalPipeline
from rag.application.retrieval.ports import PipelineObserver
from rag.application.retrieval.record import RecordRetrievalSet
from rag.application.retrieval.refiner import QueryRefiner
from rag.application.retrieval.required_input import CheckRequiredInputs, RequiredInputChecker
from rag.application.retrieval.scope import ScopeProvider
from rag.application.retrieval.search import SearchDocuments
from rag.application.shared.gateway import ModelGateway, TokenCounter
from rag.application.shared.ports import TransactionRunner
from rag.infrastructure.cache.valkey import ValkeyCache
from rag.infrastructure.config import Settings
from rag.infrastructure.gateway.client import HttpModelGateway
from rag.infrastructure.observability.metrics import build_observer
from rag.infrastructure.persistence.generation.repositories import (
    PgQueryRefinementRepository,
    PgRetrievalSetRepository,
)
from rag.infrastructure.resilience import CircuitBreaker
from rag.infrastructure.retrieval.lexical import PgLexicalSearch
from rag.infrastructure.retrieval.qdrant import QdrantDenseSearch, QdrantPointInspector
from rag.infrastructure.tender.packs import HttpPackResolver, ValkeyPackCache
from rag.presentation.scope_dependencies import ScopeServices


@dataclass(slots=True)
class RetrievalServices:
    """The retrieval use cases, and what the generation slice reuses.

    ``pipeline`` (``RetrievalPipeline.run(identity, PipelineRequest) -> RetrievalOutcome``),
    ``recorder`` (``RecordRetrievalSet.record(identity, outcome, artefact_id=...)``), ``gateway``
    (``ModelGateway``, which generation extends with ``generate_stream``), ``tokens`` and
    ``packs`` (``PackResolver``).
    """

    search: SearchDocuments
    classify: ProposeClassification
    required_input: CheckRequiredInputs
    pipeline: RetrievalPipeline
    recorder: RecordRetrievalSet
    gateway: ModelGateway
    tokens: TokenCounter
    packs: PackResolver
    _closers: tuple[HttpModelGateway, HttpPackResolver]
    observer: PipelineObserver | None = None

    async def aclose(self) -> None:
        """Close the HTTP clients."""
        for closer in self._closers:
            await closer.aclose()


def build_retrieval_services(
    settings: Settings,
    generation_tx: TransactionRunner,
    corpus_tx: TransactionRunner,
    scope: ScopeServices,
    *,
    provider: ScopeProvider | None = None,
) -> RetrievalServices:
    """Construct the module. The Gateway and Tender clients are lazy: no I/O until a request."""
    observer = build_observer(settings)
    cache: ValkeyCache | None = scope.cache
    gateway = HttpModelGateway(settings)
    packs = HttpPackResolver(
        settings.tender_base_url, ValkeyPackCache(cache) if cache is not None else None
    )
    qdrant_breaker = CircuitBreaker(name="qdrant")  # search and entity count share it (§14.3)
    inspector = QdrantPointInspector(scope.reader, qdrant_breaker)
    checker = RequiredInputChecker(inspector, observer)
    provider = provider or scope.adapter
    refiner = QueryRefiner(
        gateway,
        cache,
        enabled=settings.refiner_query_enabled,
        deadline_seconds=settings.refiner_query_deadline_ms / 1000,
        observer=observer,
    )
    pipeline = RetrievalPipeline(
        provider,
        packs,
        gateway,
        gateway,
        QdrantDenseSearch(scope.reader, qdrant_breaker),
        PgLexicalSearch(corpus_tx, settings.lexical_chunks_table, CircuitBreaker(name="lexical")),
        refiner,
        checker,
        cache,
        PipelineConfig(
            rrf_k=settings.rrf_k,
            rerank_deadline_seconds=settings.rerank_deadline_ms / 1000,
            rerank_max_in=settings.rerank_max_in,
            rerank_max_seq_tokens=settings.rerank_max_seq_tokens,
            context_window_tokens=settings.generation_context_window_tokens,
            output_reserve_fraction=settings.context_output_reserve_fraction,
            user_input_max_fraction=settings.context_user_input_max_fraction,
            mean_chunk_tokens=settings.mean_chunk_tokens,
            candidate_overfetch=settings.candidate_overfetch,
            excerpt_chars=settings.provenance_excerpt_chars,
            embedding_profile=settings.embedding_profile,
        ),
        observer,
    )
    recorder = RecordRetrievalSet(
        generation_tx, PgRetrievalSetRepository(), PgQueryRefinementRepository()
    )
    return RetrievalServices(
        search=SearchDocuments(pipeline, recorder),
        classify=ProposeClassification(
            gateway, packs, deadline_seconds=min(settings.request_timeout_seconds, 30.0)
        ),
        required_input=CheckRequiredInputs(provider, packs, checker, observer),
        pipeline=pipeline,
        recorder=recorder,
        gateway=gateway,
        tokens=gateway,
        packs=packs,
        observer=observer,
        _closers=(gateway, packs),
    )


def get_retrieval(request: Request) -> RetrievalServices:
    """The app's retrieval services (tests override this dependency)."""
    services: RetrievalServices = request.app.state.container.retrieval
    return services
