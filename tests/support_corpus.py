"""In-memory fakes of the corpus ports, emulating the database rules the use cases lean on.

The fake enforces ``uq_coll_active`` / ``uq_gen_active`` (so a wrong cutover order fails here as
it would in Postgres), the ``record_version`` bump of ``trg_bump_record_version``, and rolls
every table back when a transaction raises.
"""

from __future__ import annotations

import copy
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

from rag.application.corpus.dtos import ModelCatalogueDto, ModelDto
from rag.application.corpus.ports import CorpusRepositories
from rag.application.shared.events import DomainEvent
from rag.domain.corpus.collection import Collection
from rag.domain.corpus.grant import CollectionGrant
from rag.domain.corpus.index_generation import IndexGeneration
from rag.domain.corpus.retention import RetentionPolicy
from rag.domain.corpus.tenant_lifecycle import TenantLifecycle, TenantState
from rag.domain.shared.enums import CollectionStatus, CollectionType, IndexGenerationStatus
from rag.domain.shared.identity import RequestIdentity

TENANT = UUID("0a3f0000-0000-7000-8000-000000000001")
OTHER_TENANT = UUID("0a3f0000-0000-7000-8000-000000000009")
USER = UUID("0a3f0000-0000-7000-8000-000000000002")
TENDER = UUID("0f2a0000-0000-7000-8000-000000000001")
NOW = datetime(2026, 4, 2, 9, 14, tzinfo=UTC)


def identity(*roles: str, tenant: UUID = TENANT) -> RequestIdentity:
    """A public-route principal."""
    return RequestIdentity(tenant_id=tenant, principal_id=USER, roles=frozenset(roles))


class State:
    """The five tables and the outbox of one fake ``llm_corpus``."""

    def __init__(self) -> None:
        self.collections: dict[UUID, Collection] = {}
        self.generations: dict[tuple[UUID, int], IndexGeneration] = {}
        self.grants: list[CollectionGrant] = []
        self.retention: dict[UUID, RetentionPolicy] = {}
        self.lifecycle: dict[UUID, TenantLifecycle] = {}
        self.outbox: list[DomainEvent] = []

    def events(self, type_: str | None = None) -> list[DomainEvent]:
        """Committed events, optionally of one type."""
        return [e for e in self.outbox if type_ is None or e.type == type_]


class FakeTx:
    """``TransactionRunner``: commit on return, restore the snapshot on raise."""

    def __init__(self, state: State) -> None:
        self.state = state
        self.read_only_calls = 0

    async def run_in_tx[T](
        self, fn: Callable[[Any], Awaitable[T]], *, read_only: bool = False
    ) -> T:
        """Run ``fn`` against the state."""
        self.read_only_calls += read_only
        snapshot = copy.deepcopy(self.state.__dict__)
        try:
            return await fn(self.state)
        except BaseException:
            self.state.__dict__.update(snapshot)
            raise


class FakeEvents:
    """``EventPublisher`` writing to the state's outbox (so a rollback drops the event)."""

    async def publish(self, tx: State, event: DomainEvent) -> None:
        """Enqueue in the transaction's state."""
        tx.outbox.append(event)


def _bump(old: Any, new: Any) -> Any:
    return replace(new, record_version=old.record_version + (0 if new == old else 1))


def _check_one_active(state: State, c: Collection) -> None:
    if c.status is CollectionStatus.ACTIVE:
        for o in state.collections.values():
            if (
                o.id != c.id
                and o.status is CollectionStatus.ACTIVE
                and (o.tenant_id, o.tender_id, o.type) == (c.tenant_id, c.tender_id, c.type)
            ):
                raise AssertionError("uq_coll_active violated")


class FakeCollections:
    """``CollectionRepository``."""

    async def get(self, tx: State, collection_id: UUID, *, lock: bool = False) -> Collection | None:
        """By id."""
        return tx.collections.get(collection_id)

    async def find_by_tender(
        self,
        tx: State,
        tenant_id: UUID,
        tender_id: UUID,
        type_: CollectionType,
        *,
        lock: bool = False,
    ) -> list[Collection]:
        """Non-superseded rows of one (tenant, tender, type)."""
        return [
            c
            for c in tx.collections.values()
            if (c.tenant_id, c.tender_id, c.type) == (tenant_id, tender_id, type_)
            and c.status is not CollectionStatus.SUPERSEDED
        ]

    async def list_stalled_promotions(self, tx: State, before: datetime) -> list[Collection]:
        """Promotion targets still provisioning, created before ``before``."""
        return [
            c
            for c in tx.collections.values()
            if c.status is CollectionStatus.PROVISIONING
            and c.promoted_from is not None
            and c.created_at is not None
            and c.created_at < before
        ]

    async def list_by_tenant(self, tx: State, tenant_id: UUID) -> list[Collection]:
        """The tenant's own rows."""
        return [c for c in tx.collections.values() if c.tenant_id == tenant_id]

    async def list_by_ids(
        self, tx: State, ids: Sequence[UUID], *, limit: int, offset: int
    ) -> list[Collection]:
        """A page."""
        rows = sorted((tx.collections[i] for i in ids if i in tx.collections), key=lambda c: c.id)
        return rows[offset : offset + limit]

    async def insert(self, tx: State, collection: Collection) -> bool:
        """``ON CONFLICT (id) DO NOTHING``."""
        if collection.id in tx.collections:
            return False
        stored = replace(collection, created_at=NOW, updated_at=NOW)
        _check_one_active(tx, stored)
        tx.collections[collection.id] = stored
        return True

    async def update(self, tx: State, collection: Collection, expected_version: int) -> int | None:
        """Guarded by ``record_version``; the trigger's bump."""
        old = tx.collections.get(collection.id)
        if old is None or old.record_version != expected_version:
            return None
        assert old.restricted == collection.restricted  # trg_collection_restricted_immutable
        new = _bump(old, replace(collection, purge_epoch=old.purge_epoch))
        _check_one_active(tx, new)
        tx.collections[collection.id] = new
        return int(new.record_version)

    async def bump_purge_epoch(self, tx: State, collection_id: UUID) -> int | None:
        """Atomic increment (also bumps the version, as the trigger does)."""
        old = tx.collections.get(collection_id)
        if old is None:
            return None
        tx.collections[collection_id] = replace(
            old, purge_epoch=old.purge_epoch + 1, record_version=old.record_version + 1
        )
        return old.purge_epoch + 1

    async def close_tender(self, tx: State, tenant_id: UUID, tender_id: UUID, at: datetime) -> int:
        """``WHERE tender_closed_at IS NULL``."""
        n = 0
        for c in list(tx.collections.values()):
            if (c.tenant_id, c.tender_id, c.type) == (
                tenant_id,
                tender_id,
                CollectionType.TENDER,
            ) and c.tender_closed_at is None:
                tx.collections[c.id] = replace(
                    c, tender_closed_at=at, record_version=c.record_version + 1
                )
                n += 1
        return n

    async def delete_tenant_rows(self, tx: State, tenant_id: UUID) -> int:
        """Delete the tenant's rows and dependants."""
        ids = {c.id for c in tx.collections.values() if c.tenant_id == tenant_id}
        for key in [k for k in tx.generations if k[0] in ids]:
            del tx.generations[key]
        for cid in ids:
            tx.retention.pop(cid, None)
            del tx.collections[cid]
        tx.grants = [g for g in tx.grants if g.collection_id not in ids]
        return len(ids)


class FakeGenerations:
    """``IndexGenerationRepository``."""

    async def get(self, tx: State, collection_id: UUID, generation: int) -> IndexGeneration | None:
        """One generation."""
        return tx.generations.get((collection_id, generation))

    async def get_active(self, tx: State, collection_id: UUID) -> IndexGeneration | None:
        """The active one."""
        return next(
            (
                g
                for (cid, _), g in tx.generations.items()
                if cid == collection_id and g.status is IndexGenerationStatus.ACTIVE
            ),
            None,
        )

    async def latest_retired_before(
        self, tx: State, collection_id: UUID, generation: int
    ) -> int | None:
        """Highest retired number below ``generation``."""
        nums = [
            n
            for (cid, n), g in tx.generations.items()
            if cid == collection_id and g.status is IndexGenerationStatus.RETIRED and n < generation
        ]
        return max(nums, default=None)

    async def insert(self, tx: State, generation: IndexGeneration) -> None:
        """Insert; the trigger admits registered, verified and active."""
        assert generation.status in {
            IndexGenerationStatus.REGISTERED,
            IndexGenerationStatus.VERIFIED,
            IndexGenerationStatus.ACTIVE,
        }
        tx.generations[(generation.collection_id, generation.generation)] = generation

    async def update(self, tx: State, generation: IndexGeneration) -> None:
        """Write; one active per collection (``uq_gen_active``)."""
        if generation.status is IndexGenerationStatus.ACTIVE:
            active = await self.get_active(tx, generation.collection_id)
            if active is not None and active.generation != generation.generation:
                raise AssertionError("uq_gen_active violated")
        tx.generations[(generation.collection_id, generation.generation)] = generation


class FakeGrants:
    """``GrantRepository``."""

    async def list_active(
        self, tx: State, collection_id: UUID, *, limit: int, offset: int
    ) -> list[CollectionGrant]:
        """A page."""
        rows = [g for g in tx.grants if g.collection_id == collection_id]
        return rows[offset : offset + limit]


class FakeRetention:
    """``RetentionRepository``."""

    async def get(
        self, tx: State, collection_id: UUID, *, lock: bool = False
    ) -> RetentionPolicy | None:
        """The policy."""
        return tx.retention.get(collection_id)

    async def insert(self, tx: State, policy: RetentionPolicy) -> RetentionPolicy | None:
        """Create."""
        if policy.collection_id in tx.retention:
            return None
        stored = replace(policy, record_version=1, updated_at=NOW)
        tx.retention[policy.collection_id] = stored
        return stored

    async def update(
        self, tx: State, policy: RetentionPolicy, expected_version: int
    ) -> RetentionPolicy | None:
        """Guarded update."""
        old = tx.retention.get(policy.collection_id)
        if old is None or old.record_version != expected_version:
            return None
        stored = _bump(old, replace(policy, updated_at=NOW))
        tx.retention[policy.collection_id] = stored
        return stored  # type: ignore[no-any-return]


class FakeLifecycle:
    """``TenantLifecycleRepository`` with ``trg_tl_purged_terminal``."""

    async def get(
        self, tx: State, tenant_id: UUID, *, lock: bool = False
    ) -> TenantLifecycle | None:
        """The row."""
        return tx.lifecycle.get(tenant_id)

    async def upsert(self, tx: State, row: TenantLifecycle) -> None:
        """Insert or update; a purged row refuses."""
        old = tx.lifecycle.get(row.tenant_id)
        assert old is None or old.state is not TenantState.PURGED, "purged is terminal"
        tx.lifecycle[row.tenant_id] = row

    async def delete(self, tx: State, tenant_id: UUID) -> None:
        """Delete; a purged row refuses."""
        old = tx.lifecycle.get(tenant_id)
        assert old is None or old.state is not TenantState.PURGED, "purged is terminal"
        tx.lifecycle.pop(tenant_id, None)


class FakeScope:
    """``ScopeResolver``: a fixed visible set."""

    def __init__(self, *ids: UUID) -> None:
        self.ids = ids

    async def visible_collection_ids(
        self, identity: RequestIdentity, *, only: UUID | None = None
    ) -> tuple[UUID, ...]:
        """The configured ids, narrowed to ``only``."""
        return tuple(i for i in self.ids if only is None or i == only)


class FakePoints:
    """``PointCounter`` with a settable count."""

    def __init__(self, count: int = 0) -> None:
        self.count = count

    async def count_tender_points(self, source: Collection) -> int:
        """The configured count."""
        return self.count


class FakeCatalogue:
    """``ModelCatalogue``."""

    async def list_models(self) -> ModelCatalogueDto:
        """One model."""
        return ModelCatalogueDto([ModelDto("gen-large-eu", "eu", ["draft"], ["pro"])], NOW)


class World:
    """State, repositories, runner and publisher wired together."""

    def __init__(self) -> None:
        self.state = State()
        self.tx = FakeTx(self.state)
        self.events = FakeEvents()
        self.repos = CorpusRepositories(
            FakeCollections(), FakeGenerations(), FakeGrants(), FakeRetention(), FakeLifecycle()
        )

    def add_collection(
        self, *, restricted: bool = False, status: CollectionStatus | None = None, **kw: Any
    ) -> Collection:
        """Seed a tender collection (and generation 1) directly."""
        c = Collection.create(
            tenant_id=kw.pop("tenant_id", TENANT),
            tender_id=kw.pop("tender_id", TENDER),
            restricted=restricted,
            **kw,
        )
        c = replace(c, status=status or c.status, created_at=NOW, updated_at=NOW)
        if c.status is CollectionStatus.FROZEN:
            c = replace(c, status_before_freeze=CollectionStatus.ACTIVE)
        self.state.collections[c.id] = c
        active = c.status is CollectionStatus.ACTIVE
        self.state.generations[(c.id, 1)] = IndexGeneration.first(
            collection_id=c.id, tenant_id=c.tenant_id, active=active, now=NOW
        )
        return c

    def add_generation(
        self, c: Collection, n: int, status: IndexGenerationStatus
    ) -> IndexGeneration:
        """Seed generation ``n`` of ``c``."""
        g = IndexGeneration(
            c.id,
            n,
            c.tenant_id,
            "emb",
            "chunk",
            status,
            NOW,
            verified_at=NOW if status is not IndexGenerationStatus.REGISTERED else None,
            activated_at=NOW
            if status in {IndexGenerationStatus.ACTIVE, IndexGenerationStatus.RETIRED}
            else None,
        )
        self.state.generations[(c.id, n)] = g
        return g


def new_id() -> UUID:
    """A random uuid."""
    return uuid4()
