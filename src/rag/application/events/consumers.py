"""Use cases behind the SQS consumers of §12.4 that the corpus and generation modules lack.

Each is a plain use case: tenant binding, schema validation, DLQ handling and the ledger are the
thin adapters' (``infrastructure.events.consumers``). The lifecycle use cases of the corpus module
(``ApplyTenantState``, ``OffboardTenant``, ``RestrictTender`` ...) are called by those adapters
directly.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from uuid import UUID

from fastapicommon.logging import get_logger

from rag.application.corpus.generations import BumpPurgeEpoch
from rag.application.corpus.lifecycle import BeginTenderPurge, CloseTender
from rag.application.corpus.ports import Clock, CorpusRepositories, utc_now
from rag.application.events.contracts import (
    TOKEN_QUOTA_RESOURCES,
    ProfileCacheRefreshed,
    build_event,
)
from rag.application.events.ports import (
    LedgerTx,
    PlacementCache,
    ProfileCache,
    RunDirectory,
    RunResumer,
    SuspendedRun,
)
from rag.application.generation.commands import AppendAuditEntry, PurgeArtefacts
from rag.application.shared.events import EventPublisher
from rag.application.shared.ports import TransactionRunner, TxHandle
from rag.domain.corpus.errors import CollectionNotFound
from rag.domain.generation import audit
from rag.domain.generation.audit import AuditEntryDraft
from rag.domain.generation.ops import ArtefactArchiveRepository
from rag.domain.generation.repositories import RetrievalSetRepository
from rag.domain.shared.enums import AuditActorType
from rag.domain.shared.errors import DomainError, ErrorCode

log = get_logger(__name__)


class ForgedEvent(Exception):
    """The event names something outside the envelope tenant's own data: refused (EVT-14)."""


@dataclass(frozen=True, slots=True)
class ProjectDocumentPurge:
    """``purge_projection``: ``DocumentPurged`` -> epoch bump, then retrieval-set markers (§13.4).

    Two transactions on two instances, no 2PC. **TX-1** (``llm_corpus``) bumps
    ``collections.purge_epoch`` first and alone: that is the fail-safe order, retrieval stops
    citing purged content from the bump on. **TX-2** (``llm_generation``) writes the
    ``processed_events`` row and the ``retrieval_set_purges`` markers together (PE-1, PE-2);
    a crash between them redelivers, and the bump is harmless to repeat.

    EVT-14: the LLD reads ``collections.tenant_id`` outside any tenant context. No role here is
    ``BYPASSRLS``, so that read would see nothing; the equivalent is to read under the event's own
    tenant and refuse when the row is absent (another tenant's row is invisible) or is not that
    tenant's (the reference ``PLATFORM`` row is readable by everyone).
    """

    corpus_tx: TransactionRunner
    repos: CorpusRepositories
    bump: BumpPurgeEpoch
    sets: RetrievalSetRepository
    placement: PlacementCache

    async def execute(
        self,
        *,
        tenant_id: UUID,
        collection_id: UUID,
        document_id: UUID,
        document_version: int,
        event_id: UUID,
        once: LedgerTx,
    ) -> None:
        """Run both transactions; ``ForgedEvent`` for a collection that is not the tenant's."""
        collection = await self.corpus_tx.run_in_tx(
            lambda tx: self.repos.collections.get(tx, collection_id), read_only=True
        )
        if collection is None or collection.tenant_id != tenant_id:
            raise ForgedEvent("DocumentPurged names a collection outside the envelope's tenant")
        try:
            await self.bump.execute(collection_id)  # TX-1
        except CollectionNotFound as exc:
            raise ForgedEvent(str(exc)) from exc
        try:
            await self.placement.evict(tenant_id, collection_id)
        except Exception:  # noqa: BLE001 - the purge epoch is the invalidation; this is a bonus
            log.warning("placement_cache_eviction_failed", exc_info=True)

        async def markers(tx: TxHandle) -> None:  # TX-2
            await self.sets.purge_for_document(
                tx,
                tenant_id=tenant_id,
                collection_id=collection_id,
                document_id=document_id,
                document_version=document_version,
                event_id=event_id,
            )

        await once(markers)


@dataclass(frozen=True, slots=True)
class InvalidateProfileCache:
    """``pack_invalidation``: clear cached packs, emit ``ProfileCacheRefreshed`` (EVT-4).

    The signal is enqueued in the same ``llm_generation`` transaction as the ledger row, so it is
    published once however often the message is redelivered.
    """

    cache: ProfileCache
    events: EventPublisher

    async def execute(
        self,
        *,
        tenant_id: UUID,
        domain: str,
        language: str,
        concern: str,
        version: str,
        event_id: UUID,
        once: LedgerTx,
    ) -> None:
        """Invalidate (idempotent), then ledger and publish together."""
        previous = await self.cache.invalidate(tenant_id, domain, language, concern)

        async def emit(tx: TxHandle) -> None:
            payload = ProfileCacheRefreshed(
                profile_ref=f"dp-{domain}-{language}",
                domain=domain,
                language=language,
                concern=concern,
                from_version=previous,
                to_version=version,
                triggered_by_event_id=str(event_id),
            )
            await self.events.publish(tx, build_event(tenant_id, "", payload))

        await once(emit)


@dataclass(frozen=True, slots=True)
class ArchiveTenderArtefacts:
    """ART-7: a terminal tender outcome makes the tender's artefacts read-only.

    Idempotent: an artefact already archived is skipped, and each ``llm.artefact.archived`` chain
    entry is keyed on ``<event>:<artefact>`` (AC-9). An artefact with no tender is never touched
    (ART-8): the query is by ``tender_id``.
    """

    tx: TransactionRunner
    repo: ArtefactArchiveRepository
    audit: AppendAuditEntry
    service_account_id: UUID
    clock: Clock = utc_now

    async def execute(self, *, tenant_id: UUID, tender_id: UUID, event_id: UUID) -> int:
        """Artefacts archived by this call."""

        async def work(tx: TxHandle) -> int:
            now, archived = self.clock(), 0
            for a in await self.repo.list_live_for_tender(tx, tenant_id, tender_id):
                if not await self.repo.mark_archived(tx, a.id, now):
                    continue
                archived += 1
                await self.audit(
                    tx,
                    AuditEntryDraft(
                        tenant_id=tenant_id,
                        occurred_at=now,
                        entry_type=audit.ARTEFACT_ARCHIVED[0],
                        action=audit.ARTEFACT_ARCHIVED[1],
                        actor_type=AuditActorType.SERVICE_ACCOUNT,
                        actor_id=self.service_account_id,
                        artefact_id=a.id,
                        target_type="artefact",
                        target_id=str(a.id),
                        source_event_type=audit.ARTEFACT_ARCHIVED[0],
                        source_event_id=f"{event_id}:{a.id}",
                        metadata={"archived_at": now.isoformat().replace("+00:00", "Z")},
                    ),
                )
            return archived

        return await self.tx.run_in_tx(work)


@dataclass(frozen=True, slots=True)
class CloseTenderCorpus:
    """``tender_lifecycle`` on a terminal outcome: record the closure (COL-14), archive (ART-7).

    Each half is idempotent; the ledger entry is written after both (corpus and generation are
    two instances, so PE-1's single transaction cannot hold).
    """

    close: CloseTender
    archive: ArchiveTenderArtefacts

    async def execute(self, *, tenant_id: UUID, tender_id: UUID, event_id: UUID) -> None:
        """Close the collection rows, then archive the artefacts."""
        await self.close.execute(tenant_id, tender_id)
        await self.archive.execute(tenant_id=tenant_id, tender_id=tender_id, event_id=event_id)


@dataclass(frozen=True, slots=True)
class PropagateTenderPurge:
    """``tender_lifecycle`` on ``TenderPurged``: collection to `purging`, artefacts deleted (ART-9).

    The corpus half goes first (it stops placement from serving a purged tender); both are
    idempotent, and a failure of the second redelivers the message, which repeats the first
    harmlessly.
    """

    begin: BeginTenderPurge
    purge: PurgeArtefacts
    service_account_id: UUID

    async def execute(self, *, tenant_id: UUID, tender_id: UUID, event_id: UUID) -> None:
        """Move the collection, then delete the tender's artefacts and S3 objects."""
        await self.begin.execute(tenant_id, tender_id)
        await self.purge.execute(
            tenant_id=tenant_id,
            tender_id=tender_id,
            event_id=event_id,
            service_account_id=self.service_account_id,
            reason="tender_purged",
        )


@dataclass(frozen=True, slots=True)
class ResumeSuspendedRuns:
    """``quota_resume``: ``TenantQuotaRestored`` -> resume the tenant's quota-suspended runs.

    Only a token ``resource`` acts (``TOKEN_QUOTA_RESOURCES``). Runs resume in small batches so a
    restored tenant does not spend its new allowance in one burst; a run the Gateway still
    refuses suspends again. The signal is refused on a run that is no longer ``suspended``, and
    the event id is ledgered (EVT-6), so a repeat resumes nothing twice. While no workflow client
    is wired nothing may be reported as resumed, so the use case refuses and the message retries
    rather than being lost.
    """

    tx: TransactionRunner
    runs: RunDirectory
    resumer: RunResumer
    batch_size: int
    pause_seconds: float = 1.0
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep

    async def _page(self, after: UUID | None) -> list[SuspendedRun]:
        async def read(tx: TxHandle) -> list[SuspendedRun]:
            return await self.runs.suspended_on_quota(tx, after=after, limit=self.batch_size)

        return await self.tx.run_in_tx(read, read_only=True)

    async def execute(self, *, resource: str) -> int:
        """Runs signalled."""
        if resource not in TOKEN_QUOTA_RESOURCES:
            return 0
        if not self.resumer.available:
            raise DomainError(
                ErrorCode.WORKFLOW_ENGINE_UNAVAILABLE, "No workflow client is wired to resume runs."
            )
        resumed, after = 0, None
        while True:
            batch = await self._page(after)
            if not batch:
                return resumed
            for run in batch:
                await self.resumer.resume(run)
                resumed += 1
            after = batch[-1].run_id
            if len(batch) < self.batch_size:
                return resumed
            await self.sleep(self.pause_seconds)
