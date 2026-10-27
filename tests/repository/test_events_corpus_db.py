"""llm_corpus side of the events slice on real Postgres: outbox grants, the sweeper's
enumeration, and the stalled-promotion sweep with its event in the same transaction."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from uuid import UUID, uuid4

import asyncpg
import pytest
from eventcommon.outbox import apply_schema
from pgcommon import Config, Pool
from pgcommon.migrate import Runner
from tests.repository.test_corpus_repositories import dsn_base  # noqa: F401  (fixture)
from tests.support_corpus import TENANT, TENDER

from rag.application.corpus.ports import CorpusRepositories
from rag.application.sweeps import sweeps as s
from rag.domain.corpus.collection import Collection
from rag.infrastructure.events.publisher import OutboxEventPublisher
from rag.infrastructure.persistence.corpus.repositories import (
    PgCollectionRepository,
    PgGrantRepository,
    PgIndexGenerationRepository,
    PgRetentionRepository,
    PgTenantLifecycleRepository,
)
from rag.infrastructure.persistence.pools import bind_tenant_scope, request_guc_provider
from rag.infrastructure.persistence.transactions import PgTransactionRunner
from rag.infrastructure.sweeps.directory import PgTenantDirectory

pytestmark = pytest.mark.integration
MIGRATIONS = Path(__file__).resolve().parents[2] / "migrations" / "corpus"
OTHER = UUID("0a3f0000-0000-7000-8000-000000000009")


async def pool_for(dsn: str, role: str, guc: bool) -> Pool:
    host = dsn.split("@", 1)[1].rsplit("/", 1)[0]
    name = dsn.rsplit("/", 1)[1]
    return await Pool.create(
        Config(
            dsn=f"postgresql://{role}:pw@{host}/{name}",
            max_conns=4,
            min_conns=1,
            pgbouncer_mode=True,
            guc_provider=request_guc_provider if guc else None,
        )
    )


@pytest.fixture
async def corpus(dsn_base: str) -> AsyncIterator[tuple[str, Pool, Pool]]:  # noqa: F811
    """A migrated llm_corpus with an outbox, a ``rag_app`` pool and a ``rag_sweeper`` pool."""
    admin = await asyncpg.connect(f"{dsn_base}/postgres")
    await admin.execute("DROP DATABASE IF EXISTS llm_corpus_events WITH (FORCE)")
    await admin.execute("CREATE DATABASE llm_corpus_events")
    await admin.close()
    url = f"{dsn_base}/llm_corpus_events"
    boot = await Pool.create(Config(dsn=url, max_conns=1, min_conns=1))
    await apply_schema(boot)
    await boot.drain_and_close()
    await Runner(url, MIGRATIONS).up()
    conn = await asyncpg.connect(url)
    for login, group in (("rag_app_login", "rag_app"), ("rag_sweeper_login", "rag_sweeper")):
        await conn.execute(
            f"DO $$ BEGIN IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = '{login}') THEN "
            f"CREATE ROLE {login} LOGIN PASSWORD 'pw' IN ROLE {group}; END IF; END $$"
        )
    await conn.close()
    app, sweeper = (
        await pool_for(url, "rag_app_login", True),
        await pool_for(url, "rag_sweeper_login", False),
    )
    yield url, app, sweeper
    await app.drain_and_close()
    await sweeper.drain_and_close()


async def test_stalled_promotion_sweep_moves_the_target_and_enqueues_in_one_transaction(
    corpus: tuple[str, Pool, Pool],
) -> None:
    url, app, sweeper = corpus
    source = Collection.create(tenant_id=TENANT, tender_id=TENDER, restricted=False)
    target = Collection.create(
        tenant_id=TENANT,
        tender_id=TENDER,
        restricted=True,
        promoted_from=source.id,
        migration_id=uuid4(),
    )
    other = Collection.create(tenant_id=OTHER, tender_id=uuid4(), restricted=False)
    repos = CorpusRepositories(
        PgCollectionRepository(),
        PgIndexGenerationRepository(),
        PgGrantRepository(),
        PgRetentionRepository(),
        PgTenantLifecycleRepository(),
    )
    tx = PgTransactionRunner(app)
    with bind_tenant_scope(TENANT):
        for c in (source, target):
            await tx.run_in_tx(lambda t, c=c: repos.collections.insert(t, c))  # type: ignore[misc]
    with bind_tenant_scope(OTHER):
        await tx.run_in_tx(lambda t: repos.collections.insert(t, other))
    admin = await asyncpg.connect(url)
    await admin.execute("UPDATE collections SET created_at = now() - interval '2 days'")
    now = await admin.fetchval("SELECT now()")
    directory = PgTenantDirectory(sweeper, sweeper)
    assert await directory.tenants_with_work(s.STALLED_PROMOTIONS, now) == [TENANT]
    with pytest.raises(
        asyncpg.InsufficientPrivilegeError
    ):  # column grants: no placement, no writes
        await sweeper.with_conn(lambda cn: cn.fetch("SELECT physical_collection FROM collections"))
    sweep = s.StalledPromotions(
        tx,
        repos,
        OutboxEventPublisher(),
        timeout_seconds=86400,
        directory=directory,
        scope=bind_tenant_scope,
        dr_mode=False,
    )
    out = await sweep.run()
    assert out.ok and out.affected == 1
    assert (
        await admin.fetchval("SELECT status FROM collections WHERE id = $1", target.id)
        == "migration_failed"
    )
    assert (
        await admin.fetchval("SELECT status FROM collections WHERE id = $1", source.id)
        == "provisioning"
    )
    [event] = await admin.fetch("SELECT event_type, payload FROM outbox_events")
    assert (
        event["event_type"] == "CollectionStatusChanged" and "migration_failed" in event["payload"]
    )
    await admin.close()
    assert (await sweep.run()).affected == 0  # a second run finds nothing
