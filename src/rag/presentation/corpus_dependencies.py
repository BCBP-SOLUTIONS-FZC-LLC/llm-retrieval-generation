"""Wiring of the corpus module: use cases over ``llm_corpus``, and the route dependencies."""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from fastapi import Depends, Request

from rag.application.corpus.generations import (
    ActivateIndexGeneration,
    BumpPurgeEpoch,
    ReportMigrationResult,
)
from rag.application.corpus.lifecycle import (
    ApplyTenantState,
    BeginTenderPurge,
    CloseTender,
    OffboardTenant,
    PurgeTenantCorpus,
    RestrictTender,
)
from rag.application.corpus.models import GetModelCatalogue
from rag.application.corpus.ports import (
    CorpusRepositories,
    ModelCatalogue,
    PointCounter,
    ScopeResolver,
)
from rag.application.corpus.queries import GetCollection, ListCollections, ListGrants
from rag.application.corpus.resolve import GetCollectionPlacement, ResolveOrCreateCollection
from rag.application.corpus.retention import GetRetention, PutRetention
from rag.application.shared.events import EventPublisher
from rag.application.shared.ports import TransactionRunner
from rag.infrastructure.config import Settings
from rag.infrastructure.corpus_placeholders import FailClosedScopeResolver, UnavailablePointCounter
from rag.infrastructure.events.publisher import OutboxEventPublisher
from rag.infrastructure.gateway.model_catalogue import HttpModelCatalogue
from rag.infrastructure.persistence.corpus.repositories import (
    PgCollectionRepository,
    PgGrantRepository,
    PgIndexGenerationRepository,
    PgRetentionRepository,
    PgTenantLifecycleRepository,
)


@dataclass(slots=True)
class CorpusServices:
    """Every corpus use case. The last six are invoked by the events slice's consumers."""

    resolve: ResolveOrCreateCollection
    placement: GetCollectionPlacement
    activate: ActivateIndexGeneration
    bump_purge_epoch: BumpPurgeEpoch
    migration_result: ReportMigrationResult
    get_retention: GetRetention
    put_retention: PutRetention
    list_collections: ListCollections
    get_collection: GetCollection
    list_grants: ListGrants
    models: GetModelCatalogue
    apply_tenant_state: ApplyTenantState
    offboard_tenant: OffboardTenant
    purge_tenant_corpus: PurgeTenantCorpus
    restrict_tender: RestrictTender
    begin_tender_purge: BeginTenderPurge
    close_tender: CloseTender


def build_corpus_services(
    settings: Settings,
    tx: TransactionRunner,
    *,
    repos: CorpusRepositories | None = None,
    events: EventPublisher | None = None,
    scope: ScopeResolver | None = None,
    points: PointCounter | None = None,
    catalogue: ModelCatalogue | None = None,
) -> CorpusServices:
    """Wire the use cases over ``tx`` (the ``llm_corpus`` runner).

    The keyword arguments replace the defaults. ``corpusscope`` supplies ``scope`` and ``points``
    when it lands (the defaults fail closed); tests supply fakes, ``repos`` and ``events`` too.
    """
    repos = repos or CorpusRepositories(
        collections=PgCollectionRepository(),
        generations=PgIndexGenerationRepository(),
        grants=PgGrantRepository(),
        retention=PgRetentionRepository(),
        lifecycle=PgTenantLifecycleRepository(),
    )
    events = events or OutboxEventPublisher()
    scope = scope or FailClosedScopeResolver()
    points = points or UnavailablePointCounter()
    namespace = UUID(settings.collection_namespace)
    return CorpusServices(
        resolve=ResolveOrCreateCollection(tx, repos, events, namespace=namespace),
        placement=GetCollectionPlacement(tx, repos),
        activate=ActivateIndexGeneration(tx, repos, events),
        bump_purge_epoch=BumpPurgeEpoch(tx, repos),
        migration_result=ReportMigrationResult(tx, repos, events, points),
        get_retention=GetRetention(tx, repos),
        put_retention=PutRetention(tx, repos, scope),
        list_collections=ListCollections(tx, repos, scope),
        get_collection=GetCollection(tx, repos, scope),
        list_grants=ListGrants(tx, repos, scope, bound_seconds=settings.scope_cache_ttl_seconds),
        models=GetModelCatalogue(catalogue or HttpModelCatalogue(settings)),
        apply_tenant_state=ApplyTenantState(tx, repos, events),
        offboard_tenant=OffboardTenant(tx, repos, events),
        purge_tenant_corpus=PurgeTenantCorpus(
            tx, repos, soak_days=settings.tenant_purge_soak_days, dr_mode=settings.dr_mode
        ),
        restrict_tender=RestrictTender(tx, repos, events, namespace=namespace),
        begin_tender_purge=BeginTenderPurge(tx, repos, events),
        close_tender=CloseTender(tx, repos, events),
    )


def get_corpus_services(request: Request) -> CorpusServices:
    """The app's corpus use cases (tests override this dependency with fakes)."""
    services: CorpusServices = request.app.state.container.corpus
    return services


CorpusDep = Depends(get_corpus_services)
