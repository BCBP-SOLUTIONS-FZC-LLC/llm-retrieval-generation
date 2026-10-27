"""Corpus repositories, triggers and RLS against a real Postgres 18 (testcontainers).

Needs Docker; skipped otherwise. Statements run as ``rag_app`` with ``app.tenant_id`` bound
for the transaction, exactly as the service's pool does, so RLS and the role-keyed trigger
apply. The container's superuser runs the migrations and plays the "other role".
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import replace
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import asyncpg
import pytest
from eventcommon.outbox import apply_schema
from pgcommon import Config, Pool
from pgcommon.migrate import Runner
from tests.support_corpus import NOW, TENANT, TENDER, USER

from rag.application.corpus.commands import ActivateGenerationCommand, ResolveCollectionCommand
from rag.application.corpus.generations import ActivateIndexGeneration
from rag.application.corpus.ports import CorpusRepositories
from rag.application.corpus.resolve import ResolveOrCreateCollection
from rag.application.shared.events import DomainEvent
from rag.domain.corpus.collection import PLATFORM, REFERENCE, Collection
from rag.domain.corpus.index_generation import IndexGeneration
from rag.domain.corpus.retention import RetentionPolicy
from rag.domain.corpus.tenant_lifecycle import TenantLifecycle, TenantState
from rag.domain.shared.enums import CollectionStatus as C
from rag.domain.shared.enums import IndexGenerationStatus as G
from rag.infrastructure.events.publisher import OutboxEventPublisher
from rag.infrastructure.persistence.corpus.repositories import (
    PgCollectionRepository,
    PgGrantRepository,
    PgIndexGenerationRepository,
    PgRetentionRepository,
    PgTenantLifecycleRepository,
)

pytestmark = pytest.mark.integration

MIGRATIONS = Path(__file__).resolve().parents[2] / "migrations"

OTHER = UUID("0a3f0000-0000-7000-8000-000000000009")
REPOS = CorpusRepositories(
    PgCollectionRepository(),
    PgIndexGenerationRepository(),
    PgGrantRepository(),
    PgRetentionRepository(),
    PgTenantLifecycleRepository(),
)


@pytest.fixture(scope="module")
def dsn_base() -> str:
    """A throwaway Postgres 18; skips the module when Docker is unavailable.

    ``RAG_TEST_PG_DSN_BASE`` points the tests at an existing server instead (as the generation
    repository tests allow).
    """
    external = os.environ.get("RAG_TEST_PG_DSN_BASE")
    if external:
        yield external
        return
    pytest.importorskip("testcontainers.postgres")
    from testcontainers.postgres import PostgresContainer

    try:
        container = PostgresContainer("postgres:18", username="test", password="test")
        container.start()
    except Exception as exc:  # no Docker daemon
        pytest.skip(f"Docker unavailable: {exc}")
    yield f"postgresql://test:test@{container.get_container_host_ip()}:{container.get_exposed_port(5432)}"
    container.stop()


@pytest.fixture
async def dsn(dsn_base: str) -> AsyncIterator[str]:
    """A fresh ``llm_corpus`` with every corpus migration applied and the outbox on it."""
    admin = await asyncpg.connect(f"{dsn_base}/postgres")
    await admin.execute("DROP DATABASE IF EXISTS llm_corpus_repo")
    await admin.execute("CREATE DATABASE llm_corpus_repo")
    await admin.close()
    url = f"{dsn_base}/llm_corpus_repo"
    pool = await Pool.create(Config(dsn=url, max_conns=1, min_conns=1))
    try:
        await apply_schema(pool)  # first: migration 0003 grants on the outbox (as migrate_all)
    finally:
        await pool.drain_and_close()
    await Runner(url, MIGRATIONS / "corpus").up()
    yield url


class AppTx:
    """A ``TransactionRunner`` that behaves like the service's pool: rag_app + tenant GUC."""

    def __init__(self, dsn: str, tenant: UUID | None) -> None:
        self.dsn, self.tenant = dsn, tenant

    async def run_in_tx[T](
        self, fn: Callable[[Any], Awaitable[T]], *, read_only: bool = False
    ) -> T:
        """One connection, one transaction, ``SET LOCAL ROLE rag_app``."""
        conn = await asyncpg.connect(self.dsn, statement_cache_size=0)
        try:
            async with conn.transaction():
                await conn.execute("SET LOCAL ROLE rag_app")
                if self.tenant is not None:
                    await conn.execute(
                        "SELECT set_config('app.tenant_id', $1, true)", str(self.tenant)
                    )
                return await fn(conn)
        finally:
            await conn.close()


def app(dsn: str, tenant: UUID | None = TENANT) -> AppTx:
    """Runner for ``tenant``."""
    return AppTx(dsn, tenant)


class Recorder:
    """Publisher that records instead of writing the outbox."""

    def __init__(self) -> None:
        self.events: list[DomainEvent] = []

    async def publish(self, tx: Any, event: DomainEvent) -> None:
        self.events.append(event)


async def _seed(
    dsn: str,
    tenant: UUID = TENANT,
    tender: UUID = TENDER,
    *,
    restricted: bool = False,
    status: C | None = None,
    **kw: Any,
) -> Collection:
    c = Collection.create(tenant_id=tenant, tender_id=tender, restricted=restricted, **kw)
    if status:
        c = replace(c, status=status)

    async def work(tx: Any) -> None:
        assert await REPOS.collections.insert(tx, c)
        await REPOS.generations.insert(
            tx,
            IndexGeneration.first(
                collection_id=c.id, tenant_id=tenant, active=c.status is C.ACTIVE, now=NOW
            ),
        )

    await app(dsn, tenant).run_in_tx(work)
    return c


# --- C-I1 -----------------------------------------------------------------------------------


async def test_resolve_creates_the_row_and_a_verified_generation_one(dsn: str) -> None:
    """COL-11 / IG-3: derived id stored, generation 1 verified, written by rag_app."""
    rec = Recorder()
    result = await ResolveOrCreateCollection(app(dsn), REPOS, rec).execute(
        ResolveCollectionCommand(TENANT, TENDER)
    )
    assert result.created and result.placement.status == "provisioning"

    async def read(tx: Any) -> tuple[Any, Any]:
        return (
            await tx.fetchrow(
                "SELECT * FROM collections WHERE id = $1", result.placement.collection_id
            ),
            await tx.fetchrow(
                "SELECT * FROM index_generations WHERE collection_id = $1",
                result.placement.collection_id,
            ),
        )

    row, gen = await app(dsn).run_in_tx(read)
    assert (row["record_version"], row["purge_epoch"], row["active_index_generation"]) == (1, 0, 1)
    assert (gen["generation"], gen["status"], gen["verified_at"] is not None) == (
        1,
        "verified",
        True,
    )


async def test_two_concurrent_first_documents_create_one_row(dsn: str) -> None:
    """COL-11: both calls compute the same primary key; one 201, one 200, one row."""
    results = await asyncio.gather(
        *(
            ResolveOrCreateCollection(app(dsn), REPOS, Recorder()).execute(
                ResolveCollectionCommand(TENANT, TENDER)
            )
            for _ in range(2)
        )
    )
    assert sorted(r.created for r in results) == [False, True]
    assert results[0].placement.collection_id == results[1].placement.collection_id

    async def count(tx: Any) -> int:
        return int(await tx.fetchval("SELECT count(*) FROM collections"))

    assert await app(dsn).run_in_tx(count) == 1


# --- RLS ------------------------------------------------------------------------------------


async def test_tenant_a_cannot_see_or_write_tenant_b_rows(dsn: str) -> None:
    """RLS-3 on collections: zero rows across tenants, and a foreign insert is rejected."""
    c = await _seed(dsn)

    async def visible(tx: Any) -> list[Any]:
        return list(await tx.fetch("SELECT id FROM collections"))

    assert await app(dsn, OTHER).run_in_tx(visible) == []
    assert await app(dsn, None).run_in_tx(visible) == []  # no GUC: nothing matches
    assert [r["id"] for r in await app(dsn).run_in_tx(visible)] == [c.id]

    foreign = Collection.create(tenant_id=OTHER, tender_id=uuid4(), restricted=False)

    async def insert(tx: Any) -> bool:
        return await REPOS.collections.insert(tx, foreign)

    with pytest.raises(asyncpg.PostgresError):  # WITH CHECK refuses another tenant's row
        await app(dsn).run_in_tx(insert)


async def test_every_table_isolates_by_tenant(dsn: str) -> None:
    """The uniform policy on generations, retention and lifecycle."""
    c = await _seed(dsn)

    async def fill(tx: Any) -> None:
        await REPOS.retention.insert(tx, RetentionPolicy(c.id, TENANT, 30, False, USER))
        await REPOS.lifecycle.upsert(tx, TenantLifecycle(TENANT, TenantState.FROZEN, NOW))

    await app(dsn).run_in_tx(fill)

    async def counts(tx: Any) -> list[int]:
        return [
            int(await tx.fetchval(f"SELECT count(*) FROM {t}"))
            for t in ("index_generations", "retention_policies", "tenant_lifecycle")
        ]

    assert await app(dsn, OTHER).run_in_tx(counts) == [0, 0, 0]
    assert await app(dsn).run_in_tx(counts) == [1, 1, 1]


async def test_reference_collection_is_readable_by_every_tenant_and_writable_by_none(
    dsn: str,
) -> None:
    """The read/write pair (§4.8.1): the PLATFORM sentinel is admitted on SELECT only."""
    admin = await asyncpg.connect(dsn)
    ref = Collection.create(tenant_id=TENANT, tender_id=REFERENCE, restricted=False)
    assert ref.tenant_id == PLATFORM
    await admin.execute("SELECT set_config('app.tenant_id', $1, false)", str(PLATFORM))
    await admin.execute("SET ROLE rag_app")
    inserted = await REPOS.collections.insert(admin, ref)  # rag_app with the sentinel bound
    await admin.close()
    assert inserted  # the platform itself (GUC = PLATFORM) may write; no tenant can

    async def read(tx: Any) -> Any:
        return await REPOS.collections.get(tx, ref.id)

    assert (await app(dsn, TENANT).run_in_tx(read)).id == ref.id
    assert (await app(dsn, OTHER).run_in_tx(read)).id == ref.id

    async def write(tx: Any) -> Any:
        return await tx.execute("UPDATE collections SET purge_epoch = 9 WHERE id = $1", ref.id)

    assert (await app(dsn, TENANT).run_in_tx(write)) == "UPDATE 0"


# --- triggers and constraints ---------------------------------------------------------------


async def test_restricted_is_immutable(dsn: str) -> None:
    """COL-2: ``trg_collection_restricted_immutable`` refuses the change with 23514."""
    c = await _seed(dsn)

    async def flip(tx: Any) -> None:
        await tx.execute("UPDATE collections SET restricted = true WHERE id = $1", c.id)

    with pytest.raises(asyncpg.CheckViolationError):
        await app(dsn).run_in_tx(flip)


async def test_record_version_bumps_on_change_and_not_on_a_no_op(dsn: str) -> None:
    """§4.8.2 / §14.1: server-side bump; the version predicate guards the write."""
    c = await _seed(dsn)

    async def work(tx: Any) -> tuple[int | None, int | None, int | None]:
        no_op = await REPOS.collections.update(tx, c, 1)
        changed = await REPOS.collections.update(tx, replace(c, status=C.ACTIVE), 1)
        stale = await REPOS.collections.update(tx, replace(c, status=C.ACTIVE), 1)
        return no_op, changed, stale

    assert await app(dsn).run_in_tx(work) == (1, 2, None)


async def test_freeze_check_constraint(dsn: str) -> None:
    """COL-12 / ck_coll_freeze: frozen without status_before_freeze is refused."""
    c = await _seed(dsn)

    async def bad(tx: Any) -> None:
        await tx.execute("UPDATE collections SET status = 'frozen' WHERE id = $1", c.id)

    with pytest.raises(asyncpg.CheckViolationError):
        await app(dsn).run_in_tx(bad)

    async def good(tx: Any) -> Collection | None:
        await REPOS.collections.update(tx, c.freeze(), 1)
        return await REPOS.collections.get(tx, c.id)

    frozen = await app(dsn).run_in_tx(good)
    assert frozen is not None and (frozen.status, frozen.status_before_freeze) == (
        C.FROZEN,
        C.PROVISIONING,
    )


async def test_promotion_rows_and_the_unique_indexes(dsn: str) -> None:
    """COL-9: original + target coexist; a second target and a second active row do not."""
    src = await _seed(dsn, status=C.ACTIVE)
    tgt = await _seed(dsn, restricted=True, promoted_from=src.id, migration_id=uuid4())

    other_target = replace(tgt, id=uuid4())

    async def dup(tx: Any) -> None:
        await REPOS.collections.insert(tx, other_target)

    with pytest.raises(asyncpg.UniqueViolationError):  # uq_coll_tender
        await app(dsn).run_in_tx(dup)

    async def two_active(tx: Any) -> None:
        await REPOS.collections.update(tx, replace(tgt, status=C.ACTIVE), 1)

    with pytest.raises(asyncpg.UniqueViolationError):  # uq_coll_active: supersede first
        await app(dsn).run_in_tx(two_active)


async def test_generation_transition_trigger(dsn: str) -> None:
    """IG-5: verified -> active only; skipped or reversed steps and wrong roles are 23514."""
    c = await _seed(dsn)

    async def skip(tx: Any) -> None:
        await tx.execute(
            "UPDATE index_generations SET status = 'retired' WHERE collection_id = $1", c.id
        )

    with pytest.raises(asyncpg.CheckViolationError):
        await app(dsn).run_in_tx(skip)

    async def insert_building(tx: Any) -> None:
        await REPOS.generations.insert(
            tx, IndexGeneration(c.id, 2, TENANT, "e", "c", G.BUILDING, NOW, verified_at=NOW)
        )

    with pytest.raises(asyncpg.CheckViolationError):  # IG-3: created registered, verified or active
        await app(dsn).run_in_tx(insert_building)

    # A superuser is a member of every role, so it would pass the membership check: a plain role
    # that is neither rag_app nor rag_activity stands in for "another service".
    admin = await asyncpg.connect(dsn)
    try:
        await admin.execute(
            "DO $$ BEGIN IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'outsider') "
            "THEN CREATE ROLE outsider NOLOGIN; END IF; END $$"
        )
        await admin.execute("GRANT SELECT, INSERT ON index_generations TO outsider")
        await admin.execute("SET ROLE outsider")
        await admin.execute("SELECT set_config('app.tenant_id', $1, false)", str(TENANT))
        with pytest.raises(asyncpg.PostgresError, match="by outsider"):
            await admin.execute(
                "INSERT INTO index_generations (collection_id, generation, tenant_id, "
                "embedding_profile, chunker_version, status, verified_at) "
                "VALUES ($1, 5, $2, 'e', 'c', 'verified', now())",
                c.id,
                TENANT,
            )
    finally:
        await admin.close()


async def test_purged_tenant_lifecycle_is_terminal(dsn: str) -> None:
    """TL-1: ``trg_tl_purged_terminal`` refuses UPDATE and DELETE of a purged row."""

    async def purge(tx: Any) -> None:
        await REPOS.lifecycle.upsert(tx, TenantLifecycle(TENANT, TenantState.FROZEN, NOW))
        await REPOS.lifecycle.upsert(tx, TenantLifecycle(TENANT, TenantState.PURGED, NOW))

    await app(dsn).run_in_tx(purge)

    async def undo(tx: Any) -> None:
        await REPOS.lifecycle.upsert(tx, TenantLifecycle(TENANT, TenantState.FROZEN, NOW))

    with pytest.raises(asyncpg.CheckViolationError):
        await app(dsn).run_in_tx(undo)

    async def delete(tx: Any) -> None:
        await REPOS.lifecycle.delete(tx, TENANT)

    with pytest.raises(asyncpg.CheckViolationError):
        await app(dsn).run_in_tx(delete)


# --- C-I4 with the real outbox --------------------------------------------------------------


async def test_activation_writes_state_and_event_row_in_one_transaction(dsn: str) -> None:
    """EVT-8 / IG-1: the outbox row is on the same connection; a retry adds none."""
    c = await _seed(dsn, status=C.ACTIVE)

    async def register(tx: Any) -> None:
        await REPOS.generations.insert(
            tx,
            IndexGeneration(
                c.id, 2, TENANT, "emb-m3-1024", "c-1.9.0", G.VERIFIED, NOW, verified_at=NOW
            ),
        )

    await app(dsn).run_in_tx(register)
    use = ActivateIndexGeneration(app(dsn), REPOS, OutboxEventPublisher())
    first = await use.execute(ActivateGenerationCommand(TENANT, c.id, 2, 1))
    assert (first.active_index_generation, first.previous) == (2, 1)
    again = await use.execute(ActivateGenerationCommand(TENANT, c.id, 2, 99))
    assert again.active_index_generation == 2

    admin = await asyncpg.connect(dsn)
    rows = await admin.fetch("SELECT event_type FROM outbox_events")
    states = await admin.fetch(
        "SELECT generation, status FROM index_generations ORDER BY generation"
    )
    await admin.close()
    assert [r["event_type"] for r in rows] == ["IndexGenerationActivated"]
    assert [(r["generation"], r["status"]) for r in states] == [(1, "retired"), (2, "active")]


# --- other repository behaviour -------------------------------------------------------------


async def test_purge_epoch_and_close_tender_and_retention_roundtrip(dsn: str) -> None:
    """C-I6 atomic increment, COL-14 once-only close, P-6 version-guarded update."""
    c = await _seed(dsn)

    async def work(tx: Any) -> tuple[Any, ...]:
        epochs = [await REPOS.collections.bump_purge_epoch(tx, c.id) for _ in range(2)]
        closed = await REPOS.collections.close_tender(tx, TENANT, TENDER, NOW)
        again = await REPOS.collections.close_tender(tx, TENANT, TENDER, NOW)
        made = await REPOS.retention.insert(tx, RetentionPolicy(c.id, TENANT, 30, False, USER))
        dup = await REPOS.retention.insert(tx, RetentionPolicy(c.id, TENANT, 31, False, USER))
        stale = await REPOS.retention.update(tx, replace(made, retain_days=60), 7)  # type: ignore[arg-type]
        ok = await REPOS.retention.update(tx, replace(made, retain_days=60), 1)  # type: ignore[arg-type]
        return epochs, closed, again, dup, stale, ok.record_version if ok else None  # type: ignore[union-attr]

    epochs, closed, again, dup, stale, version = await app(dsn).run_in_tx(work)
    assert (epochs, closed, again, dup, stale, version) == ([1, 2], 1, 0, None, None, 2)


async def test_tenant_row_deletion_handles_the_promotion_foreign_key(dsn: str) -> None:
    """§21.1 phase two: targets before sources; leaves tenant_lifecycle; spares other tenants."""
    src = await _seed(dsn, status=C.ACTIVE)
    await _seed(dsn, restricted=True, promoted_from=src.id, migration_id=uuid4())
    mine = await _seed(dsn, tender=uuid4())
    theirs = await _seed(dsn, tenant=OTHER)

    async def purge(tx: Any) -> int:
        await REPOS.retention.insert(tx, RetentionPolicy(mine.id, TENANT, 30, False, USER))
        await REPOS.lifecycle.upsert(tx, TenantLifecycle(TENANT, TenantState.PURGED, NOW))
        return await REPOS.collections.delete_tenant_rows(tx, TENANT)

    assert await app(dsn).run_in_tx(purge) == 3

    async def left(tx: Any) -> tuple[int, int]:
        return (
            int(await tx.fetchval("SELECT count(*) FROM collections")),
            int(await tx.fetchval("SELECT count(*) FROM tenant_lifecycle")),
        )

    assert await app(dsn).run_in_tx(left) == (0, 1)
    assert await app(dsn, OTHER).run_in_tx(left) == (1, 0)
    assert theirs.id  # the other tenant's row survived


async def test_lookup_by_tender_skips_superseded_and_lists_by_tenant_exclude_platform(
    dsn: str,
) -> None:
    """The repository lookups the use cases rely on."""
    src = await _seed(dsn, status=C.ACTIVE)

    async def supersede(tx: Any) -> None:
        await REPOS.collections.update(tx, src.supersede(), 1)

    await app(dsn).run_in_tx(supersede)
    assert (
        await app(dsn).run_in_tx(
            lambda tx: REPOS.collections.find_by_tender(tx, TENANT, TENDER, src.type)
        )
        == []
    )
    rows = await app(dsn).run_in_tx(lambda tx: REPOS.collections.list_by_tenant(tx, TENANT))
    assert [r.status for r in rows] == [C.SUPERSEDED]
