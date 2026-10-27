"""Use cases invoked by the tenant and tender lifecycle consumers (§12.4, §13.5, §13.8, §21.1).

They are plain, idempotent use cases. Envelope validation (EVT-14), the ``processed_events``
ledger and the tenant GUC are the events slice's; the repositories filter on the tenant
explicitly as well, so a tenant's rows are the only ones touched.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from uuid import UUID, uuid4

from rag.application.corpus.events import (
    StatusReason,
    collection_promoted,
    collection_provisioned,
    collection_status_changed,
)
from rag.application.corpus.ports import Clock, CorpusRepositories, utc_now
from rag.application.shared.events import EventPublisher
from rag.application.shared.ports import TransactionRunner, TxHandle
from rag.domain.corpus.collection import COLLECTION_NAMESPACE, Collection
from rag.domain.corpus.errors import OptimisticLockConflict
from rag.domain.corpus.index_generation import IndexGeneration
from rag.domain.corpus.tenant_lifecycle import (
    TenantAction,
    TenantLifecycle,
    TenantState,
    action_for_status,
)
from rag.domain.shared.enums import CollectionStatus, CollectionType

_FROZEN_OR_LIVE = {
    CollectionStatus.PROVISIONING,
    CollectionStatus.ACTIVE,
    CollectionStatus.MIGRATION_FAILED,
}


class _Base:
    def __init__(
        self,
        tx: TransactionRunner,
        repos: CorpusRepositories,
        events: EventPublisher,
        *,
        clock: Clock = utc_now,
    ) -> None:
        self._tx, self._r, self._events, self._clock = tx, repos, events, clock

    async def _move(
        self, tx: TxHandle, before: Collection, after: Collection, reason: StatusReason
    ) -> None:
        if await self._r.collections.update(tx, after, before.record_version) is None:
            raise OptimisticLockConflict("The collection changed; the consumer retries.")
        await self._events.publish(tx, collection_status_changed(before, after, reason))


class ApplyTenantState(_Base):
    """``tenant_state`` consumer: freeze, or restore ``status_before_freeze`` (COL-12, TL-2)."""

    async def execute(self, tenant_id: UUID, status: str, cause: str | None = None) -> TenantAction:
        """Apply IAM's ``status``; ``past_due``, ``cancelled`` and ``offboarded`` change nothing."""
        action = action_for_status(status)
        if action is TenantAction.IGNORE:
            return action

        async def work(tx: TxHandle) -> None:
            row = await self._r.lifecycle.get(tx, tenant_id, lock=True)
            if row is not None and row.purged:  # terminal: a late reactivation changes nothing
                return
            rows = await self._r.collections.list_by_tenant(tx, tenant_id)
            if action is TenantAction.FREEZE:
                for c in rows:
                    if c.status in _FROZEN_OR_LIVE:
                        await self._move(tx, c, c.freeze(), StatusReason.TENANT_SUSPENDED)
                frozen = (
                    row.refreeze(cause)
                    if row
                    else TenantLifecycle(tenant_id, TenantState.FROZEN, self._clock(), cause)
                )
                await self._r.lifecycle.upsert(tx, frozen)
            else:
                for c in rows:
                    if c.status is CollectionStatus.FROZEN:
                        await self._move(tx, c, c.restore(), StatusReason.TENANT_REACTIVATED)
                if row is not None:
                    await self._r.lifecycle.delete(tx, tenant_id)

        await self._tx.run_in_tx(work)
        return action


class OffboardTenant(_Base):
    """``tenant_offboarding`` phase one: collections to `purging`, lifecycle to `purged` (EVT-15).

    Reference collections are never the tenant's, so they are excluded (SCOPE-9). Nothing is
    deleted here: phase two is ``PurgeTenantCorpus`` after the soak.
    """

    async def execute(self, tenant_id: UUID, cause: str | None = None) -> None:
        """One transaction; a repeat changes nothing and keeps the first ``changed_at``."""

        async def work(tx: TxHandle) -> None:
            for c in await self._r.collections.list_by_tenant(tx, tenant_id):
                if c.status not in {CollectionStatus.PURGING, CollectionStatus.SUPERSEDED}:
                    await self._move(tx, c, c.begin_purge(), StatusReason.PURGE_STARTED)
            row = await self._r.lifecycle.get(tx, tenant_id, lock=True)
            if row is not None and row.purged:
                return
            now = self._clock()
            purged = (
                row.purge(now, cause)
                if row
                else TenantLifecycle(tenant_id, TenantState.PURGED, now, cause)
            )
            await self._r.lifecycle.upsert(tx, purged)

        await self._tx.run_in_tx(work)


class PurgeTenantCorpus:
    """``tenant_offboarding`` phase two: delete the rows once the soak has elapsed (§21.1, TL-4)."""

    def __init__(
        self,
        tx: TransactionRunner,
        repos: CorpusRepositories,
        *,
        soak_days: int,
        dr_mode: bool = False,
        clock: Clock = utc_now,
    ) -> None:
        self._tx, self._r = tx, repos
        self._soak_days, self._dr_mode, self._clock = soak_days, dr_mode, clock

    async def execute(self, tenant_id: UUID) -> bool:
        """Delete collections and dependants, leaving ``tenant_lifecycle``; ``False`` if not due.

        Disabled by ``DR_MODE`` like the other destructive jobs (EVT-15).
        """
        if self._dr_mode:
            return False

        async def work(tx: TxHandle) -> bool:
            row = await self._r.lifecycle.get(tx, tenant_id, lock=True)
            if row is None or not row.soak_elapsed(self._clock(), self._soak_days):
                return False
            await self._r.collections.delete_tenant_rows(tx, tenant_id)
            return True

        return await self._tx.run_in_tx(work)


class RestrictionOutcome(StrEnum):
    """What a ``TenderRestricted`` delivery did."""

    CREATED_RESTRICTED = "created_restricted"
    PROMOTION_STARTED = "promotion_started"
    ALREADY_RESTRICTED = "already_restricted"
    ANNOUNCED_ONLY = "announced_only"


class RestrictTender(_Base):
    """``tender_lifecycle`` consumer, ``TenderRestricted``: announce and promote (COL-7, COL-8)."""

    def __init__(
        self,
        tx: TransactionRunner,
        repos: CorpusRepositories,
        events: EventPublisher,
        *,
        namespace: UUID = COLLECTION_NAMESPACE,
        clock: Clock = utc_now,
    ) -> None:
        super().__init__(tx, repos, events, clock=clock)
        self._namespace = namespace

    async def execute(self, tenant_id: UUID, tender_id: UUID) -> RestrictionOutcome:
        """One transaction; a redelivery is a no-op through ``uq_coll_tender`` (LD-33)."""
        return await self._tx.run_in_tx(lambda tx: self._run(tx, tenant_id, tender_id))

    async def _run(self, tx: TxHandle, tenant_id: UUID, tender_id: UUID) -> RestrictionOutcome:
        rows = await self._r.collections.find_by_tender(
            tx, tenant_id, tender_id, CollectionType.TENDER, lock=True
        )
        source = next((c for c in rows if not c.restricted), None)
        has_restricted = any(c.restricted for c in rows)
        now = self._clock()

        if source is None:
            if has_restricted:
                return RestrictionOutcome.ALREADY_RESTRICTED
            # Restriction arrived before any document: one restricted row, no promotion (LD-43).
            created = Collection.create(
                tenant_id=tenant_id, tender_id=tender_id, restricted=True, namespace=self._namespace
            )
            if await self._r.collections.insert(tx, created):
                await self._first_generation(tx, created, now)
                await self._events.publish(tx, collection_provisioned(created))
            return RestrictionOutcome.CREATED_RESTRICTED

        announced = source.announce_restricted(now)  # authorization switches here (COL-8)
        if announced is not source:
            version = await self._r.collections.update(tx, announced, source.record_version)
            if version is None:
                raise OptimisticLockConflict("The collection changed; the consumer retries.")
        if has_restricted:
            return RestrictionOutcome.ALREADY_RESTRICTED
        if source.status not in {CollectionStatus.ACTIVE, CollectionStatus.PROVISIONING}:
            return RestrictionOutcome.ANNOUNCED_ONLY

        target = Collection.create(
            tenant_id=tenant_id,
            tender_id=tender_id,
            restricted=True,
            namespace=self._namespace,
            promoted_from=source.id,
            migration_id=uuid4(),
        )
        if not await self._r.collections.insert(tx, target):
            return RestrictionOutcome.ALREADY_RESTRICTED
        await self._first_generation(tx, target, now)
        await self._events.publish(tx, collection_promoted(target))
        return RestrictionOutcome.PROMOTION_STARTED

    async def _first_generation(self, tx: TxHandle, c: Collection, now: datetime) -> None:
        await self._r.generations.insert(
            tx,
            IndexGeneration.first(collection_id=c.id, tenant_id=c.tenant_id, active=False, now=now),
        )


class BeginTenderPurge(_Base):
    """``tender_lifecycle`` consumer, ``TenderPurged``: to `purging`, never deleted (COL-13)."""

    async def execute(self, tenant_id: UUID, tender_id: UUID) -> int:
        """Rows moved; a repeat moves none."""

        async def work(tx: TxHandle) -> int:
            moved = 0
            rows = await self._r.collections.find_by_tender(
                tx, tenant_id, tender_id, CollectionType.TENDER, lock=True
            )
            for c in rows:
                if c.status is not CollectionStatus.PURGING:
                    await self._move(tx, c, c.begin_purge(), StatusReason.PURGE_STARTED)
                    moved += 1
            return moved

        return await self._tx.run_in_tx(work)


class CloseTender(_Base):
    """``tender_lifecycle`` consumer, a terminal outcome: set ``tender_closed_at`` once (COL-14)."""

    async def execute(self, tenant_id: UUID, tender_id: UUID) -> int:
        """Rows closed, whatever their status; a later outcome closes none."""
        return await self._tx.run_in_tx(
            lambda tx: self._r.collections.close_tender(tx, tenant_id, tender_id, self._clock())
        )
