"""Dependency container: plain functions over ``app.state`` (no DI framework).

Later slices add fields to ``Container`` (repositories, clients) and a getter below; routes
take them with ``Depends``.
"""

from __future__ import annotations

from dataclasses import dataclass

from fastapi import Request
from pgcommon import Pool

from rag.application.shared.ports import TransactionRunner
from rag.infrastructure.config import Settings
from rag.infrastructure.persistence.corpus.transactions import CorpusTransactionRunner
from rag.infrastructure.persistence.generation.probes import CorpusTenderGate
from rag.infrastructure.persistence.transactions import PgTransactionRunner
from rag.infrastructure.storage.artefact_content_store import S3ArtefactContentStore
from rag.presentation.corpus_dependencies import CorpusServices, build_corpus_services
from rag.presentation.generate_dependencies import GenerateServices, build_generate_services
from rag.presentation.generation_dependencies import GenerationServices, build_generation_services
from rag.presentation.retrieval_dependencies import RetrievalServices, build_retrieval_services
from rag.presentation.scope_dependencies import ScopeServices, build_scope_services
from rag.presentation.workflows_dependencies import WorkflowServices, build_workflow_services


@dataclass(slots=True)
class Container:
    """Process-wide collaborators built once in the lifespan, held on ``app.state.container``."""

    settings: Settings
    corpus_pool: Pool
    generation_pool: Pool
    corpus_tx: TransactionRunner
    generation_tx: TransactionRunner
    generation: GenerationServices
    corpus: CorpusServices
    scope: ScopeServices
    retrieval: RetrievalServices
    generate: GenerateServices
    workflows: WorkflowServices

    async def aclose(self) -> None:
        """Close the clients the container owns (Gateway, Tender, Valkey, Qdrant).

        Producers whose client dropped are tied to no request, so they are awaited first (up to
        ``REQUEST_TIMEOUT_SECONDS``): closing the Gateway client under them would turn a
        finished generation into a failed run and lose its artefact (GEN-2).
        """
        await self.generate.drain(self.settings.request_timeout_seconds)
        await self.retrieval.aclose()
        await self.scope.aclose()


def build_container(settings: Settings, corpus_pool: Pool, generation_pool: Pool) -> Container:
    """Wire the container from the two open pools."""
    generation_tx = PgTransactionRunner(generation_pool)
    corpus_runner = CorpusTransactionRunner(corpus_pool)
    scope = build_scope_services(settings, corpus_runner)
    store = S3ArtefactContentStore(
        settings.s3_bucket,
        region=settings.s3_region,
        endpoint_url=settings.s3_endpoint_url,
        kms_key_id=settings.s3_kms_key_id,
    )
    retrieval = build_retrieval_services(settings, generation_tx, corpus_runner, scope)
    generate = build_generate_services(
        settings, generation_tx, corpus_runner, retrieval, scope, store
    )
    return Container(
        settings=settings,
        corpus_pool=corpus_pool,
        generation_pool=generation_pool,
        corpus_tx=PgTransactionRunner(corpus_pool),
        generation_tx=generation_tx,
        generation=build_generation_services(
            settings, generation_tx, scope=scope.adapter, store=store
        ),
        corpus=build_corpus_services(
            settings, corpus_runner, scope=scope.adapter, points=scope.points
        ),
        scope=scope,
        retrieval=retrieval,
        generate=generate,
        workflows=build_workflow_services(
            settings,
            generation_tx,
            generate,
            retrieval,
            scope.adapter,
            store,
            CorpusTenderGate(corpus_runner),
        ),
    )


def get_container(request: Request) -> Container:
    """The app's container."""
    container: Container = request.app.state.container
    return container


def get_settings(request: Request) -> Settings:
    """The app's settings (available without the lifespan, unlike the container)."""
    settings: Settings = request.app.state.settings
    return settings
