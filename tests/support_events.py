"""Fakes and a wired ``ConsumerServices`` for the consumer and event use-case tests."""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from uuid import UUID

from eventcommon import Envelope, new_envelope

from rag.application.corpus.generations import BumpPurgeEpoch
from rag.application.corpus.lifecycle import (
    ApplyTenantState,
    BeginTenderPurge,
    CloseTender,
    OffboardTenant,
    RestrictTender,
)
from rag.application.events.consumers import (
    ArchiveTenderArtefacts,
    CloseTenderCorpus,
    InvalidateProfileCache,
    ProjectDocumentPurge,
    PropagateTenderPurge,
    ResumeSuspendedRuns,
)
from rag.application.events.ports import SuspendedRun
from rag.application.generation.commands import AppendAuditEntry, PurgeArtefacts
from rag.application.shared.events import DomainEvent
from rag.domain.generation.artefact import Artefact
from rag.domain.generation.ops import ArchivableArtefact
from rag.domain.generation.retrieval import RetrievalSetView
from rag.domain.shared.enums import ArtefactArchiveState
from rag.infrastructure.events.consumers import ConsumerServices
from tests.support_corpus import NOW, TENANT, World
from tests.support_generation import (
    FakeAuditRepo,
    FakePurgeRepo,
    FakeRetrievalRepo,
    FakeStore,
    FakeTx,
    FixedClock,
)

SERVICE_ACCOUNT = UUID("0a3f0000-0000-7000-8000-0000000000ff")


def envelope(type_: str, data: dict[str, Any], tenant: str | UUID = TENANT) -> Envelope[Any]:
    """A well-formed inbound envelope."""
    return new_envelope(type_, "someone-else", data, tenant_id=str(tenant))


class FakeLedger:
    """``eventcommon.inbox.Store`` for one consumer: ``process`` is exactly-once per id."""

    def __init__(self, consumer: str = "c") -> None:
        self.consumer = consumer
        self.done: set[UUID] = set()
        self.fail_next_effect = False

    async def is_processed(self, event_id: UUID) -> bool:
        return event_id in self.done

    async def mark_processed(self, event_id: UUID) -> None:
        self.done.add(event_id)

    async def process(self, env: Envelope[Any], fn: Callable[[Any], Awaitable[None]]) -> None:
        event_id = uuid.UUID(env.id)
        if event_id in self.done:
            return
        await fn(None)
        if self.fail_next_effect:  # the transaction rolls back: no ledger row either
            self.fail_next_effect = False
            raise RuntimeError("TX-2 failed")
        self.done.add(event_id)


class ListEvents:
    """``EventPublisher`` recording generation-side events."""

    def __init__(self) -> None:
        self.events: list[DomainEvent] = []

    async def publish(self, tx: Any, event: DomainEvent) -> None:
        self.events.append(event)


class FakeCache:
    """``ProfileCache`` and ``PlacementCache`` in one recorder."""

    def __init__(self, version: str | None = "4.1.0", fail_evict: bool = False) -> None:
        self.version, self.fail_evict = version, fail_evict
        self.invalidated: list[tuple[Any, ...]] = []
        self.evicted: list[tuple[UUID, UUID]] = []

    async def invalidate(
        self, tenant_id: UUID, domain: str, language: str, concern: str
    ) -> str | None:
        self.invalidated.append((tenant_id, domain, language, concern))
        return self.version

    async def evict(self, tenant_id: UUID, collection_id: UUID) -> None:
        if self.fail_evict:
            raise RuntimeError("valkey down")
        self.evicted.append((tenant_id, collection_id))


class FakeArchiveRepo:
    """``ArtefactArchiveRepository`` over a list of artefacts."""

    def __init__(self, artefacts: list[Artefact]) -> None:
        self.artefacts = artefacts

    async def list_live_for_tender(
        self, tx: Any, tenant_id: UUID, tender_id: UUID
    ) -> list[ArchivableArtefact]:
        return [
            ArchivableArtefact(a.id, tender_id)
            for a in self.artefacts
            if a.tenant_id == tenant_id
            and a.tender_id == tender_id
            and a.archive_state is ArtefactArchiveState.LIVE
        ]

    async def mark_archived(self, tx: Any, artefact_id: UUID, at: datetime) -> bool:
        for a in self.artefacts:
            if a.id == artefact_id and a.archive_state is ArtefactArchiveState.LIVE:
                a.archive(at)
                return True
        return False


class FakeRuns:
    """``RunDirectory`` and ``RunResumer`` in one."""

    def __init__(self, runs: list[SuspendedRun], available: bool = True) -> None:
        self.runs, self.available = runs, available
        self.resumed: list[UUID] = []
        self.pages = 0

    async def suspended_on_quota(
        self, tx: Any, *, after: UUID | None, limit: int
    ) -> list[SuspendedRun]:
        self.pages += 1
        rest = [
            r
            for r in sorted(self.runs, key=lambda r: r.run_id)
            if after is None or r.run_id > after
        ]
        return rest[:limit]

    async def resume(self, run: SuspendedRun) -> None:
        self.resumed.append(run.run_id)


@dataclass
class Bench:
    """Everything a consumer test needs, wired over in-memory fakes."""

    world: World = field(default_factory=World)
    chain: FakeAuditRepo = field(default_factory=FakeAuditRepo)
    retrieval: FakeRetrievalRepo = field(default_factory=FakeRetrievalRepo)
    cache: FakeCache = field(default_factory=FakeCache)
    events: ListEvents = field(default_factory=ListEvents)
    runs: FakeRuns = field(default_factory=lambda: FakeRuns([]))
    artefacts: list[Artefact] = field(default_factory=list)
    store: FakeStore = field(default_factory=FakeStore)

    def __post_init__(self) -> None:
        w, gen_tx = self.world, FakeTx()
        audit = AppendAuditEntry(self.chain)
        self.purge_repo = FakePurgeRepo([])
        self.purge_repo.artefacts = self.artefacts  # one list: tests seed ``bench.artefacts``
        self.archive_repo = FakeArchiveRepo(self.artefacts)
        purge = PurgeArtefacts(gen_tx, self.purge_repo, audit, self.store, FixedClock())
        archive = ArchiveTenderArtefacts(
            gen_tx, self.archive_repo, audit, SERVICE_ACCOUNT, clock=lambda: NOW
        )

        async def no_sleep(_: float) -> None:
            return None

        self.services = ConsumerServices(
            apply_tenant_state=ApplyTenantState(w.tx, w.repos, w.events),
            offboard_tenant=OffboardTenant(w.tx, w.repos, w.events),
            restrict_tender=RestrictTender(w.tx, w.repos, w.events),
            close_tender=CloseTenderCorpus(CloseTender(w.tx, w.repos, w.events), archive),
            tender_purge=PropagateTenderPurge(
                BeginTenderPurge(w.tx, w.repos, w.events), purge, SERVICE_ACCOUNT
            ),
            document_purge=ProjectDocumentPurge(
                w.tx, w.repos, BumpPurgeEpoch(w.tx, w.repos), self.retrieval, self.cache
            ),
            profile_invalidation=InvalidateProfileCache(self.cache, self.events),
            quota_resume=ResumeSuspendedRuns(
                gen_tx, self.runs, self.runs, batch_size=2, sleep=no_sleep
            ),
        )


def view(
    collection: UUID, document: UUID, version: int = 1, tenant: UUID = TENANT
) -> RetrievalSetView:
    """A retrieval set citing ``document`` v``version`` in ``collection``."""
    from dataclasses import replace

    from tests.support_generation import make_retrieval_view

    v = make_retrieval_view(collection)
    chunks = tuple(
        replace(
            ch,
            tenant_id=tenant,
            document_id=document,
            document_version=version,
            collection_id=collection,
        )
        for ch in v.chunks
    )
    return replace(v, chunks=chunks)
