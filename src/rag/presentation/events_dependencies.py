"""Wiring of the event workers: relays, consumers and the new use cases they call (§12, §18.3).

Registered with ``register_background_worker`` when ``EVENTS_ENABLED=true``. The consumers call
the use cases already built on ``Container.corpus`` / ``Container.generation``; only the use cases
that did not exist before (document-purge projection, pack invalidation, archive, tender purge
propagation, quota resume) are constructed here.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from uuid import UUID

from eventcommon import metrics
from fastapi import FastAPI
from redis.asyncio import Redis

from rag.application.corpus.ports import CorpusRepositories
from rag.application.events.consumers import (
    ArchiveTenderArtefacts,
    CloseTenderCorpus,
    InvalidateProfileCache,
    ProjectDocumentPurge,
    PropagateTenderPurge,
    ResumeSuspendedRuns,
)
from rag.infrastructure.cache.invalidation import ValkeyPlacementCache, ValkeyProfileCache
from rag.infrastructure.config import Settings
from rag.infrastructure.events.consumers import ConsumerServices
from rag.infrastructure.events.publisher import OutboxEventPublisher
from rag.infrastructure.events.relay import start_relays
from rag.infrastructure.events.runtime import start_consumers
from rag.infrastructure.persistence.corpus.repositories import (
    PgCollectionRepository,
    PgGrantRepository,
    PgIndexGenerationRepository,
    PgRetentionRepository,
    PgTenantLifecycleRepository,
)
from rag.infrastructure.persistence.generation.ops import (
    PgArtefactArchiveRepository,
    PgRunDirectory,
)
from rag.infrastructure.persistence.generation.repositories import PgRetrievalSetRepository
from rag.presentation.dependencies import Container


def build_consumer_services(
    settings: Settings, container: Container, valkey: Redis
) -> ConsumerServices:
    """The use cases behind the six consumers."""
    corpus, generation = container.corpus, container.generation
    account = UUID(settings.service_account_id)
    repos = CorpusRepositories(
        collections=PgCollectionRepository(),
        generations=PgIndexGenerationRepository(),
        grants=PgGrantRepository(),
        retention=PgRetentionRepository(),
        lifecycle=PgTenantLifecycleRepository(),
    )
    archive = ArchiveTenderArtefacts(
        container.generation_tx, PgArtefactArchiveRepository(), generation.append_audit, account
    )
    return ConsumerServices(
        apply_tenant_state=corpus.apply_tenant_state,
        offboard_tenant=corpus.offboard_tenant,
        restrict_tender=corpus.restrict_tender,
        close_tender=CloseTenderCorpus(corpus.close_tender, archive),
        tender_purge=PropagateTenderPurge(
            corpus.begin_tender_purge, generation.purge_artefacts, account
        ),
        document_purge=ProjectDocumentPurge(
            container.corpus_tx,
            repos,
            corpus.bump_purge_epoch,
            PgRetrievalSetRepository(),
            ValkeyPlacementCache(valkey),
        ),
        profile_invalidation=InvalidateProfileCache(
            ValkeyProfileCache(valkey), OutboxEventPublisher()
        ),
        quota_resume=ResumeSuspendedRuns(
            container.generation_tx,
            PgRunDirectory(),
            container.workflows.resumer,
            batch_size=settings.quota_resume_batch_size,
        ),
    )


async def start_event_workers(app: FastAPI) -> Callable[[], Awaitable[None]]:
    """Start the relays, then the consumers; the returned stop reverses that order."""
    container: Container = app.state.container
    settings = container.settings
    if settings.enable_metrics:  # eventcommon's platform_* outbox / consumer / DLQ metrics
        identity = metrics.metrics_identity_from_env("platform", settings.app_name)
        metrics.init_metrics(identity)
    relays = start_relays(
        settings, {"generation": container.generation_pool, "corpus": container.corpus_pool}
    )
    valkey: Redis = Redis.from_url(settings.valkey_url)
    consumers = start_consumers(
        settings, build_consumer_services(settings, container, valkey), container.generation_pool
    )
    app.state.relay_ready = relays.ready  # the readiness probe's input (§18.1)

    async def stop() -> None:
        await consumers.stop()
        await relays.stop()
        await valkey.aclose()

    return stop
