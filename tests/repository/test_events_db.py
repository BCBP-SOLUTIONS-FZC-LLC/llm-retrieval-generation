# ruff: noqa: E501 (SQL literals)
"""The events slice against real Postgres: outbox grants and relay, the sweeper role, the sweep
statements, and one sweep end to end through the request-less tenant scope."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import timedelta
from uuid import UUID, uuid4

import asyncpg
import pytest
from eventcommon.inbox import Store
from eventcommon.mock import MockPublisher
from eventcommon.outbox import PostgresStore, Runner, RunnerConfig
from pgcommon import Config, Pool
from tests.repository import seed
from tests.repository.support import Db, as_tenant
from tests.support_events import SERVICE_ACCOUNT

from rag.application.events import contracts as c
from rag.application.events.consumers import ArchiveTenderArtefacts
from rag.application.generation.commands import AppendAuditEntry
from rag.application.sweeps import sweeps as s
from rag.domain.generation.audit import first_break
from rag.infrastructure.events.publisher import OutboxEventPublisher
from rag.infrastructure.events.routing import RoutingPublisher
from rag.infrastructure.persistence.generation.ops import (
    PgArtefactArchiveRepository,
    PgRunDirectory,
    PgSweepRepository,
)
from rag.infrastructure.persistence.generation.repositories import PgAuditChainRepository
from rag.infrastructure.persistence.pools import bind_tenant_scope, request_guc_provider
from rag.infrastructure.persistence.transactions import PgTransactionRunner
from rag.infrastructure.sweeps.directory import PgTenantDirectory

pytestmark = pytest.mark.integration

A = UUID("0a3f0000-0000-7000-8000-00000000000a")
B = UUID("0a3f0000-0000-7000-8000-00000000000b")
TENDER = UUID("0a3f0000-0000-7000-8000-0000000000a1")
PW = "pw"


async def login_pool(db: Db, role: str, *, scoped: bool = False) -> Pool:
    host = db.admin_dsn.split("@", 1)[1].rsplit("/", 1)[0]
    name = db.admin_dsn.rsplit("/", 1)[1]
    return await Pool.create(
        Config(
            dsn=f"postgresql://{role}:{PW}@{host}/{name}",
            max_conns=4,
            min_conns=1,
            pgbouncer_mode=True,
            guc_provider=request_guc_provider if scoped else None,
            pool_name=role,
        )
    )


@pytest.fixture
async def sweeper(db: Db) -> AsyncIterator[Pool]:
    """A login that is a member of ``rag_sweeper`` and of nothing else."""
    admin = await db.admin()
    await admin.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'rag_sweeper_login') "
        f"THEN CREATE ROLE rag_sweeper_login LOGIN PASSWORD '{PW}' IN ROLE rag_sweeper; END IF; END $$"
    )
    await admin.close()
    pool = await login_pool(db, "rag_sweeper_login")
    yield pool
    await pool.drain_and_close()


@pytest.fixture
async def scoped_app(db: Db) -> AsyncIterator[Pool]:
    """``rag_app`` with the service's real provider (request context, else the bound scope)."""
    pool = await login_pool(db, "rag_app_login", scoped=True)
    yield pool
    await pool.drain_and_close()


async def admin_sql(db: Db, *statements: tuple[str, tuple[object, ...]]) -> None:
    conn = await db.admin()
    try:
        for sql, args in statements:
            await conn.execute(sql, *args)
    finally:
        await conn.close()


async def artefact(
    db: Db,
    tenant: UUID,
    *,
    status: str = "generating",
    tender: UUID | None = TENDER,
    run_status: str | None = "generating",
    age: str = "2 hours",
    kind: str = "document",
) -> tuple[UUID, UUID | None]:
    """An artefact (and one run) whose timestamps are ``age`` old; triggers off to backdate."""
    conn = await db.admin()
    try:
        await conn.execute("ALTER TABLE artefacts DISABLE TRIGGER trg_touch_updated_at")
        await conn.execute("ALTER TABLE generation_runs DISABLE TRIGGER trg_touch_updated_at")
        art = await conn.fetchval(
            "INSERT INTO artefacts (tenant_id, tender_id, kind, capability, created_by, status, "
            "created_at, updated_at) VALUES ($1, $2, $3::artefact_kind, 'technical_proposal', $4, "
            f"$5::artefact_status, now() - interval '{age}', now() - interval '{age}') RETURNING id",
            tenant,
            tender,
            kind,
            seed.USER,
            status,
        )
        run = None
        if run_status:
            run = await conn.fetchval(
                "INSERT INTO generation_runs (artefact_id, tenant_id, tender_id, principal_id, "
                "requested_mode, effective_mode, eligibility_state, domain_profile_version, "
                "language_pack_version, status, workflow_id, current_stage, created_at, updated_at) "
                "VALUES ($1, $2, $3, $4, 'assistance', 'assistance', 'GENERATABLE', 'dp-1', 'lp-1', "
                f"$5::run_status, $6, 'stage2', now() - interval '{age}', now() - interval '{age}') "
                "RETURNING id",
                art,
                tenant,
                tender,
                seed.USER,
                run_status,
                f"wf-{art}",
            )
        return art, run
    finally:
        await conn.close()


# --- outbox: grants and the relay ------------------------------------------------------------
async def test_enqueue_as_rag_app_relay_routes_by_name_and_prune_works(db: Db) -> None:
    """EVT-8 on llm_generation with the real grants; one relay, two topics, nothing scattered."""
    generation_event = c.build_event(
        A,
        c.tender_subject(TENDER),
        c.GenerationCancelled("a", "g", "p", "superseded", 1, 2, True, tender_id=str(TENDER)),
    )
    index_event = c.build_event(
        A, "", c.ProfileCacheRefreshed("dp-civil-en", "civil", "en", "sections", "4.2.0", "e1")
    )

    async def enqueue(tx: object) -> None:
        for event in (generation_event, index_event):
            await OutboxEventPublisher().publish(tx, event)

    await db.app_tx.run_in_tx(enqueue)
    gen, idx = MockPublisher(), MockPublisher()
    runner = Runner(
        RunnerConfig(pool=db.app, publisher=RoutingPublisher(gen, idx), poll_interval=0.05)
    )
    task = asyncio.create_task(runner.start())
    try:
        async with asyncio.timeout(10):
            await runner.ready.wait()
            while not (gen.published and idx.published):
                await asyncio.sleep(0.05)
    finally:
        warning = await runner.stop()
        await task
    assert warning is None
    assert [e.type for e in gen.published] == ["GenerationCancelled"]
    assert [e.type for e in idx.published] == ["ProfileCacheRefreshed"]
    assert (
        gen.published[0].source == "llm-rag-service"
        and gen.published[0].subject == f"tenders/{TENDER}"
    )
    await asyncio.sleep(0.05)
    assert await PostgresStore(db.app).prune_published(timedelta(milliseconds=1), 100) == 2


async def test_rag_activity_may_enqueue_but_not_read_claim_or_delete(db: Db) -> None:
    """OBX-3: the Worker Pool enqueues onto this table and runs no relay."""
    event = c.build_event(A, "", c.ProfileCacheRefreshed("p", "d", "l", "c", "1", "e"))
    await db.activity_tx.run_in_tx(lambda tx: OutboxEventPublisher().publish(tx, event))
    for sql in (
        "SELECT 1 FROM outbox_events",
        "UPDATE outbox_events SET attempts = 9",
        "DELETE FROM outbox_events",
    ):
        with pytest.raises(asyncpg.InsufficientPrivilegeError):
            await db.activity_tx.run_in_tx(lambda tx, q=sql: tx.execute(q))  # type: ignore[misc]


# --- the sweeper role ------------------------------------------------------------------------
async def test_the_sweeper_lists_tenants_that_rag_app_cannot_and_nothing_more(
    db: Db, sweeper: Pool
) -> None:
    await artefact(db, A, status="generating")
    await artefact(db, B, status="generating")
    await artefact(db, B, status="complete", run_status=None)
    directory = PgTenantDirectory(sweeper, sweeper)
    now = (await (await db.admin()).fetchval("SELECT now()")) + timedelta(hours=1)
    assert set(await directory.tenants_with_work(s.STALLED_GENERATIONS, now)) == {A, B}
    assert await directory.tenants_with_work(s.ORPHAN_ARTEFACTS, now) == []
    # the same statement as rag_app with no tenant bound sees nothing (RLS, no BYPASSRLS)
    assert (
        await db.app.with_conn(
            lambda cn: cn.fetch(
                "SELECT DISTINCT tenant_id FROM artefacts WHERE status = 'generating'"
            )
        )
        == []
    )
    # column grants: no content columns, and no writes
    with pytest.raises(asyncpg.InsufficientPrivilegeError):
        await sweeper.with_conn(lambda cn: cn.fetch("SELECT capability FROM artefacts"))
    with pytest.raises(asyncpg.InsufficientPrivilegeError):
        await sweeper.with_conn(lambda cn: cn.execute("UPDATE artefacts SET status = 'failed'"))
    with pytest.raises(asyncpg.InsufficientPrivilegeError):
        await sweeper.with_conn(lambda cn: cn.fetch("SELECT * FROM artefact_sections"))
    for name in (s.IDEMPOTENCY_KEYS, s.RETRIEVAL_SETS, s.GRAPH_CHECKPOINTS, s.AUDIT_CHAIN):
        await directory.tenants_with_work(name, now)  # every enumeration statement is permitted


# --- statements ------------------------------------------------------------------------------
async def test_stalled_and_pending_selection_and_fail_artefact(db: Db) -> None:
    quiet, run = await artefact(db, A, status="generating", run_status="generating")
    recent, _ = await artefact(db, A, status="generating", run_status="generating", age="1 minute")
    pending, _ = await artefact(db, A, status="pending", run_status="pending", age="1 hour")
    repo, tx = PgSweepRepository(), db.app_tx
    now = await (await db.admin()).fetchval("SELECT now()")
    with as_tenant(A):
        stalled = await tx.run_in_tx(
            lambda t: repo.stalled_artefacts(t, now - timedelta(minutes=15))
        )
        orphans = await tx.run_in_tx(
            lambda t: repo.pending_artefacts(t, now - timedelta(minutes=5))
        )
        assert [x.artefact_id for x in stalled] == [quiet] and stalled[0].run_id == run
        assert [x.artefact_id for x in orphans] == [pending] and orphans[0].workflow_id
        failed = await tx.run_in_tx(lambda t: repo.fail_artefact(t, quiet, now))
        assert failed is not None and failed.tenant_id == A and failed.run_id == run
        assert failed.failed_at_stage == "stage2" and (
            failed.sections_complete,
            failed.sections_total,
        ) == (0, 0)
        assert await tx.run_in_tx(lambda t: repo.fail_artefact(t, quiet, now)) is None  # moved
        assert await tx.run_in_tx(lambda t: repo.fail_artefact(t, recent, now)) is not None
    conn = await db.admin()
    row = await conn.fetchrow(
        "SELECT status, failure_reason, failed_at_stage FROM generation_runs WHERE id = $1", run
    )
    assert (row["status"], row["failure_reason"], row["failed_at_stage"]) == (
        "failed",
        "internal",
        "stage2",
    )
    assert await conn.fetchval("SELECT status FROM artefacts WHERE id = $1", quiet) == "failed"
    await conn.close()
    with as_tenant(B):  # another tenant sees and fails nothing of A's
        assert await tx.run_in_tx(lambda t: repo.fail_artefact(t, pending, now)) is None


async def test_a_live_run_keeps_a_quiet_artefact_out_of_the_stalled_sweep(db: Db) -> None:
    art, run = await artefact(db, A, status="generating", run_status="generating")
    await admin_sql(db, ("UPDATE generation_runs SET updated_at = now() WHERE id = $1", (run,)))
    now = await (await db.admin()).fetchval("SELECT now()")
    with as_tenant(A):
        found = await db.app_tx.run_in_tx(
            lambda t: PgSweepRepository().stalled_artefacts(t, now - timedelta(minutes=15))
        )
    assert found == []


async def test_idempotency_keys_orphan_sets_and_checkpoints(db: Db) -> None:
    art, run = await artefact(db, A, status="complete", run_status="complete")
    repo, tx = PgSweepRepository(), db.app_tx
    conn = await db.admin()
    await conn.execute(
        "UPDATE generation_runs SET idempotency_key = 'k', request_hash = 'h' WHERE id = $1", run
    )
    await conn.execute("ALTER TABLE retrieval_sets DISABLE TRIGGER trg_rs_immutable")
    old_orphan = await seed.retrieval_set(conn, A, None, None)
    await seed.chunks(conn, A, old_orphan, uuid4(), uuid4())
    young_orphan = await seed.retrieval_set(conn, A, None, None)
    await conn.execute(
        "UPDATE retrieval_sets SET created_at = now() - interval '40 days' WHERE id = $1",
        old_orphan,
    )
    refinement = await conn.fetchval(
        "INSERT INTO query_refinements (tenant_id, retrieval_set_id, principal_hash, original_query, "
        "pack_version, refiner_version, latency_ms, outcome) VALUES ($1, $2, 'h', 'q', 'p', 'r', 1, 'success') RETURNING id",
        A,
        old_orphan,
    )
    sec = await conn.fetchval(
        "INSERT INTO artefact_sections (artefact_id, tenant_id, section_type, ordinal, state, "
        "failure_reason) VALUES ($1, $2, 'intro', 1, 'failed', 'internal') RETURNING id",
        art,
        A,
    )
    live = await conn.fetchval(
        "INSERT INTO artefact_sections (artefact_id, tenant_id, section_type, ordinal, state) "
        "VALUES ($1, $2, 'body', 2, 'generating') RETURNING id",
        art,
        A,
    )
    for section in (sec, live):
        await conn.execute(
            "INSERT INTO graph_checkpoints (tenant_id, section_id, activity_run_id, state, created_at) "
            "VALUES ($1, $2, 'x', '{}'::jsonb, now() - interval '2 days')",
            A,
            section,
        )
    now = await conn.fetchval("SELECT now()")
    await conn.close()
    with as_tenant(A):
        assert (
            await tx.run_in_tx(lambda t: repo.clear_idempotency_keys(t, now - timedelta(hours=1)))
            == 1
        )
        assert (
            await tx.run_in_tx(
                lambda t: repo.delete_orphan_retrieval_sets(t, now - timedelta(days=30), 10)
            )
            == 1
        )
        assert (
            await tx.run_in_tx(
                lambda t: repo.delete_stale_checkpoints(t, now - timedelta(hours=24))
            )
            == 1
        )
    conn = await db.admin()
    assert (
        await conn.fetchval("SELECT idempotency_key FROM generation_runs WHERE id = $1", run)
        is None
    )
    assert (
        await conn.fetchval(
            "SELECT count(*) FROM retrieval_set_chunks WHERE retrieval_set_id = $1", old_orphan
        )
        == 0
    )
    assert (
        await conn.fetchval("SELECT id FROM retrieval_sets WHERE id = $1", young_orphan)
        == young_orphan
    )
    assert (
        await conn.fetchval(
            "SELECT retrieval_set_id FROM query_refinements WHERE id = $1", refinement
        )
        is None
    )
    assert (
        await conn.fetchval("SELECT count(*) FROM graph_checkpoints WHERE section_id = $1", live)
        == 1
    )
    await conn.close()


async def test_archive_marks_a_tenders_live_artefacts_once_and_never_an_answer(db: Db) -> None:
    mine, _ = await artefact(db, A, run_status=None)
    answer, _ = await artefact(db, A, tender=None, kind="answer", run_status=None)
    other_tender, _ = await artefact(db, A, tender=uuid4(), run_status=None)
    uc = ArchiveTenderArtefacts(
        db.app_tx,
        PgArtefactArchiveRepository(),
        AppendAuditEntry(PgAuditChainRepository()),
        SERVICE_ACCOUNT,
    )
    event = uuid4()
    with as_tenant(A):
        assert await uc.execute(tenant_id=A, tender_id=TENDER, event_id=event) == 1
        assert await uc.execute(tenant_id=A, tender_id=TENDER, event_id=event) == 0
        chain = await db.app_tx.run_in_tx(lambda t: PgAuditChainRepository().list_chain(t, A))
    assert [e.draft.artefact_id for e in chain] == [mine] and first_break(chain) is None
    conn = await db.admin()
    states = {
        r["id"]: r["archive_state"]
        for r in await conn.fetch("SELECT id, archive_state FROM artefacts")
    }
    await conn.close()
    assert states == {mine: "archived", answer: "live", other_tender: "live"}


async def test_run_directory_pages_quota_suspended_runs_of_the_bound_tenant(db: Db) -> None:
    runs = []
    for _ in range(3):
        _, r = await artefact(db, A, run_status="suspended")
        runs.append(r)
    await artefact(db, A, run_status="suspended")  # then make this one not quota-suspended
    await admin_sql(
        db,
        (
            "UPDATE generation_runs SET failure_reason = 'quota_exhausted' WHERE id = ANY($1::uuid[])",
            (runs,),
        ),
    )
    _, foreign = await artefact(db, B, run_status="suspended")
    await admin_sql(
        db,
        ("UPDATE generation_runs SET failure_reason = 'quota_exhausted' WHERE id = $1", (foreign,)),
    )
    directory = PgRunDirectory()
    with as_tenant(A):
        first = await db.app_tx.run_in_tx(
            lambda t: directory.suspended_on_quota(t, after=None, limit=2)
        )
        rest = await db.app_tx.run_in_tx(
            lambda t: directory.suspended_on_quota(t, after=first[-1].run_id, limit=2)
        )
    assert [r.run_id for r in first + rest] == sorted(runs) and len(rest) == 1


# --- one sweep, end to end, through the request-less tenant scope ----------------------------
async def test_the_stalled_sweep_runs_as_rag_app_under_each_tenants_bound_scope(
    db: Db, sweeper: Pool, scoped_app: Pool
) -> None:
    """Directory via ``rag_sweeper``; work via ``rag_app`` whose GUC comes from ``bind_tenant_scope``:
    state change, ``GenerationFailed`` outbox row and chain entry in one transaction per artefact."""
    art_a, _ = await artefact(db, A)
    art_b, _ = await artefact(db, B)
    tx = PgTransactionRunner(scoped_app)
    sweep = s.StalledGeneratingArtefacts(
        tx,
        PgSweepRepository(),
        OutboxEventPublisher(),
        AppendAuditEntry(PgAuditChainRepository()),
        SERVICE_ACCOUNT,
        stalled_seconds=900,
        directory=PgTenantDirectory(sweeper, sweeper),
        scope=bind_tenant_scope,
        dr_mode=False,
    )
    out = await sweep.run()
    assert out.ok and out.tenants == 2 and out.affected == 2
    conn = await db.admin()
    assert {r["id"]: r["status"] for r in await conn.fetch("SELECT id, status FROM artefacts")} == {
        art_a: "failed",
        art_b: "failed",
    }
    events = await conn.fetch("SELECT event_type, tenant_id FROM outbox_events ORDER BY created_at")
    assert {(e["event_type"], e["tenant_id"]) for e in events} == {
        ("GenerationFailed", str(A)),
        ("GenerationFailed", str(B)),
    }
    chains = await conn.fetch("SELECT tenant_id, entry_type FROM artefact_audit_chain")
    await conn.close()
    assert {(r["tenant_id"], r["entry_type"]) for r in chains} == {
        (A, "llm.generation.failed"),
        (B, "llm.generation.failed"),
    }
    again = await sweep.run()  # idempotent: nothing is stalled any more
    assert again.tenants == 0 and again.affected == 0


async def test_the_ledger_sweep_can_delete_as_rag_app(db: Db) -> None:
    """PE-1 / EVT-16: ``Store.prune`` DELETEs ``processed_events``, which 0002 never granted."""
    store = Store(db.app, "tenant_state")
    for _ in range(3):
        await store.mark_processed(uuid4())
    await asyncio.sleep(0.05)
    assert await store.prune(timedelta(milliseconds=1)) == 3
    assert await Store(db.app, "purge_projection").prune(timedelta(days=90)) == 0
