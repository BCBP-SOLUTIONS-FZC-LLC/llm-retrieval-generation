"""C-I1 resolve-or-create (§5.7.16, §13.6) and C-I2 placement (any status)."""

from __future__ import annotations

from uuid import UUID

from rag.application.corpus.commands import ResolveCollectionCommand
from rag.application.corpus.dtos import PlacementDto, ResolveResultDto
from rag.application.corpus.events import collection_provisioned
from rag.application.corpus.mappers import to_placement
from rag.application.corpus.ports import Clock, CorpusRepositories, utc_now
from rag.application.shared.events import EventPublisher
from rag.application.shared.ports import TransactionRunner, TxHandle
from rag.domain.corpus.collection import (
    COLLECTION_NAMESPACE,
    PLATFORM,
    Collection,
    collection_type_for,
    pick_resolvable,
)
from rag.domain.corpus.errors import (
    CollectionFrozen,
    CollectionNotFound,
    CollectionNotProvisioned,
    CollectionPurging,
)
from rag.domain.corpus.index_generation import IndexGeneration
from rag.domain.shared.enums import CollectionStatus, CollectionType


class ResolveOrCreateCollection:
    """C-I1: the only route that creates a collection row (COL-3)."""

    def __init__(
        self,
        tx: TransactionRunner,
        repos: CorpusRepositories,
        events: EventPublisher,
        *,
        namespace: UUID = COLLECTION_NAMESPACE,
        clock: Clock = utc_now,
    ) -> None:
        self._tx, self._r, self._events = tx, repos, events
        self._namespace, self._clock = namespace, clock

    async def execute(self, cmd: ResolveCollectionCommand) -> ResolveResultDto:
        """Refuse a frozen or purged tenant (TL-3), else return or create the row (COL-11)."""
        return await self._tx.run_in_tx(lambda tx: self._run(tx, cmd))

    async def _run(self, tx: TxHandle, cmd: ResolveCollectionCommand) -> ResolveResultDto:
        lifecycle = await self._r.lifecycle.get(tx, cmd.tenant_id)
        if lifecycle is not None:  # TL-3: before any lookup, so a purged tenant is not rebuilt
            raise CollectionPurging() if lifecycle.purged else CollectionFrozen()

        type_ = collection_type_for(cmd.tender_id)
        owner = PLATFORM if type_ is CollectionType.REFERENCE else cmd.tenant_id
        # The lookup ignores `restricted`: a stale flag must not miss a restricted row (LD-43).
        row = pick_resolvable(
            await self._r.collections.find_by_tender(tx, owner, cmd.tender_id, type_)
        )
        if row is not None:
            return self._existing(row)
        if type_ is CollectionType.REFERENCE:
            raise CollectionNotProvisioned()  # a cross-tenant corpus is a platform decision (COL-5)

        new = Collection.create(
            tenant_id=cmd.tenant_id,
            tender_id=cmd.tender_id,
            restricted=bool(cmd.tender_restricted),  # absent behaves as false (LD-97)
            namespace=self._namespace,
        )
        if not await self._r.collections.insert(tx, new):
            # Lost the race on the derived primary key: re-read the winner (COL-11).
            row = pick_resolvable(
                await self._r.collections.find_by_tender(tx, owner, cmd.tender_id, type_)
            )
            if row is None:  # the id is held by a superseded row (COL-11): a defect, not a 4xx
                raise RuntimeError(f"collection id {new.id} is taken by a superseded row")
            return self._existing(row)

        now = self._clock()
        await self._r.generations.insert(
            tx,
            IndexGeneration.first(
                collection_id=new.id,
                tenant_id=new.tenant_id,
                active=new.status is CollectionStatus.ACTIVE,
                now=now,
            ),
        )
        await self._events.publish(tx, collection_provisioned(new))
        return ResolveResultDto(to_placement(new), created=True)

    @staticmethod
    def _existing(row: Collection) -> ResolveResultDto:
        if row.status is CollectionStatus.FROZEN:
            raise CollectionFrozen()
        if row.status is CollectionStatus.PURGING:
            raise CollectionPurging()
        return ResolveResultDto(to_placement(row), created=False)


class GetCollectionPlacement:
    """C-I2: answers in every status, `purging` included (COL-13)."""

    def __init__(self, tx: TransactionRunner, repos: CorpusRepositories) -> None:
        self._tx, self._r = tx, repos

    async def execute(self, collection_id: UUID) -> PlacementDto:
        """The row's placement, or ``COLLECTION_NOT_FOUND``."""

        async def work(tx: TxHandle) -> PlacementDto:
            row = await self._r.collections.get(tx, collection_id)
            if row is None:
                raise CollectionNotFound(f"No collection {collection_id}.")
            return to_placement(row)

        return await self._tx.run_in_tx(work, read_only=True)
