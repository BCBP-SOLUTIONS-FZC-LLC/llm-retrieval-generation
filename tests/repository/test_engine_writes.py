# ruff: noqa: E501 (SQL literals)
"""The engine's write functions against real Postgres, as the ``rag_activity`` login (workers LLD §4.2).

Every function runs under the Worker Pool's own role, so a missing grant, a trigger or an RLS
policy shows up here and not in production: audit append, fact supersede, section delete and the
outbox insert are the ones that needed one.
"""

from __future__ import annotations

import contextlib
from collections.abc import AsyncIterator
from typing import Any
from uuid import UUID, uuid4

import asyncpg
import pytest
from tests.repository import seed
from tests.repository.support import Db, as_tenant
from tests.repository.test_generation_writes import verification
from tests.support_generate import Store
from tests.support_generation import FixedClock

from llm_models.workers import MintedVersion
from rag.application.generation.commands import AppendAuditEntry
from rag.domain.workflows.input import ScopeSeed, WorkflowInput
from rag.engine import documents, runs, sections
from rag.engine.context import EngineServices, RunContext, run_scope
from rag.engine.errors import ContentUnavailable, NothingToMint, RunGone, RunTerminal
from rag.infrastructure.events.publisher import OutboxEventPublisher
from rag.infrastructure.persistence.generation.engine import OutlineEntry, PgEngineRepository
from rag.infrastructure.persistence.generation.regeneration import PgFactRepository
from rag.infrastructure.persistence.generation.repositories import PgAuditChainRepository
from rag.infrastructure.persistence.generation.workflows import PgRunRepository
from rag.infrastructure.persistence.generation.writes import PgGenerationWriteRepository

pytestmark = pytest.mark.integration

A = UUID("0a3f0000-0000-7000-8000-000000000001")
PRINCIPAL = UUID("0a3f0000-0000-7000-8000-000000000002")
TENDER = UUID("0a3f0000-0000-7000-8000-0000000000b1")
OUTLINE = (
    OutlineEntry("1", "executive_summary", is_foundation=True),
    OutlineEntry("2", "methodology"),
    OutlineEntry("3", "pricing"),
)


@contextlib.asynccontextmanager
async def scoped(s: EngineServices, winput: dict[str, Any]) -> AsyncIterator[RunContext]:
    """``run_scope`` plus the fixture's own tenant provider (production's pool reads the engine's)."""
    with as_tenant(A):
        async with run_scope(s, winput) as ctx:
            yield ctx


def services(db: Db) -> tuple[EngineServices, Store]:
    store = Store()
    return (
        EngineServices(
            tx=db.activity_tx,
            runs=PgRunRepository(),
            writes=PgGenerationWriteRepository(),
            facts=PgFactRepository(),
            engine=PgEngineRepository(),
            events=OutboxEventPublisher(),
            audit=AppendAuditEntry(PgAuditChainRepository()),
            clock=FixedClock(),
            store=store,
        ),
        store,
    )


async def start_run(
    db: Db, *, gated: bool = True, origin: str = "original", status: str = "pending"
) -> tuple[dict[str, Any], UUID, UUID]:
    """An artefact and an async run (as the API's admission writes them); returns the run input."""
    run_id, art = uuid4(), uuid4()
    regenerating = origin != "original"
    base = uuid4() if regenerating else None
    winput = WorkflowInput(
        tenant_id=A,
        principal_id=PRINCIPAL,
        artefact_id=art,
        generation_run_id=run_id,
        capability="technical_proposal",
        requested_mode="assistance",
        plan="standard",
        residency_mode="standard",
        domain_profile_version="dp-1",
        language_pack_version="lp-1",
        scope_seed=ScopeSeed(TENDER),
        tender_id=TENDER,
        correlation_id="corr-1",
        outline_gate=gated,
        origin=origin,
        base_version_id=base,
        instruction="make it shorter" if regenerating else None,
    ).to_dict()
    conn = await db.admin()
    try:
        if regenerating:  # the base version need not exist for what these tests exercise
            await conn.execute("SET session_replication_role = replica")
        await conn.execute(
            "INSERT INTO artefacts (id, tenant_id, tender_id, kind, capability, created_by, status) "
            "VALUES ($1, $2, $3, 'document', 'technical_proposal', $4, $5::artefact_status)",
            art, A, TENDER, PRINCIPAL, "complete" if regenerating else "pending",
        )  # fmt: skip
        await conn.execute(
            "INSERT INTO generation_runs (id, artefact_id, tenant_id, tender_id, principal_id, "
            "origin, base_version_id, workflow_id, run_input, requested_mode, effective_mode, "
            "eligibility_state, domain_profile_version, language_pack_version, status) "
            "VALUES ($1, $2, $3, $4, $5, $6::artefact_origin, $7, $8, $9::jsonb, 'assistance', "
            "'assistance', 'GENERATABLE', 'dp-1', 'lp-1', $10::run_status)",
            run_id, art, A, TENDER, PRINCIPAL, origin, base, f"wf-{art}",
            __import__("json").dumps(winput), status,
        )  # fmt: skip
    finally:
        await conn.close()
    return winput, run_id, art


async def one(db: Db, sql: str, *args: Any) -> Any:
    conn = await db.admin()
    try:
        return await conn.fetchrow(sql, *args)
    finally:
        await conn.close()


async def event_types(db: Db) -> list[str]:
    conn = await db.admin()
    try:
        return [
            r["event_type"]
            for r in await conn.fetch("SELECT event_type FROM outbox_events ORDER BY ordering_seq")
        ]
    finally:
        await conn.close()


async def approve_as_the_api_does(db: Db, run_id: UUID) -> None:
    """W-3 wrote the row before the signal: ``generating``, ``approved``, ``stage1``."""
    conn = await db.admin()
    try:
        await conn.execute(
            "UPDATE generation_runs SET status = 'generating', outline_state = 'approved', "
            "current_stage = 'stage1' WHERE id = $1",
            run_id,
        )
    finally:
        await conn.close()


def draft(retrieval_set: UUID) -> sections.SectionDraft:
    return sections.SectionDraft(
        content=b"section text",
        retrieval_set_id=retrieval_set,
        prompt_hash="ph-1",
        model_profile_id="mp-1",
        serving_tier="primary",
        verification=verification(),
    )


async def test_a_gated_run_goes_from_outline_to_a_minted_document_as_rag_activity(db: Db) -> None:
    s, store = services(db)
    winput, run_id, art = await start_run(db, gated=True)
    async with scoped(s, winput) as ctx:
        # outline: pending -> outline_building, and the artefact leaves `pending` (P0-2)
        assert await runs.begin_stage(ctx, "outline") is True
        row = await one(
            db,
            "SELECT status, outline_state, current_stage FROM generation_runs WHERE id = $1",
            run_id,
        )
        assert tuple(row) == ("outline_building", "building", "outline")
        assert (await one(db, "SELECT status FROM artefacts WHERE id = $1", art))[
            "status"
        ] == "generating"
        assert await runs.begin_stage(ctx, "outline") is True  # a repeat changes nothing
        assert (await event_types(db)).count("GenerationProgressed") == 1

        saved = await sections.save_outline(ctx, OUTLINE, gated=True)
        assert len(saved.section_ids) == 3 and saved.gate_expires_at is not None
        row = await one(
            db,
            "SELECT status, outline_state, gate_expires_at FROM generation_runs WHERE id = $1",
            run_id,
        )
        assert (row["status"], row["outline_state"]) == ("awaiting_outline_approval", "ready")
        assert row["gate_expires_at"] is not None
        assert "OutlineReady" in await event_types(db)
        assert (
            await sections.save_outline(ctx, OUTLINE, gated=True)
        ).section_ids == saved.section_ids
        assert (await event_types(db)).count("OutlineReady") == 1  # a repeat publishes nothing

        # the API approves; stage 1 then writes the stage only, no edge (RUN-7)
        await approve_as_the_api_does(db, run_id)
        before = len(await event_types(db))
        assert await runs.begin_stage(ctx, "stage1") is True
        assert len(await event_types(db)) == before
        s1, s2, s3 = (UUID(i) for i in saved.section_ids)

        conn = await db.admin()
        try:
            rset = await seed.retrieval_set(conn, A, TENDER, art)
        finally:
            await conn.close()
        assert await sections.begin_section(ctx, s1) is True
        won = await sections.save_section(ctx, s1, draft(rset))
        assert won.state == "complete" and won.version_id is not None and not won.skipped_terminal
        assert len(store.objects) == 1
        assert (
            await sections.begin_section(ctx, s1) is False
        )  # finished by this run: skip (ASYNC-14)
        assert await documents.record_facts(ctx, [documents.FactDraft("client", "Acme", s1)]) == 1
        assert await documents.record_facts(ctx, [documents.FactDraft("client", "Acme", s1)]) == 0
        assert (
            await documents.record_facts(ctx, [documents.FactDraft("client", "Acme Ltd", s1)]) == 1
        )
        current = await one(
            db,
            "SELECT count(*) FILTER (WHERE superseded_at IS NULL) AS cur, count(*) AS n FROM document_facts WHERE artefact_id = $1",
            art,
        )
        assert (current["cur"], current["n"]) == (1, 2)  # DF-2: superseded first, then inserted

        assert await sections.begin_section(ctx, s2) is True
        failed = await sections.fail_section(ctx, s2, "internal")
        assert (failed.state, failed.failure_reason) == ("failed", "internal")
        assert await sections.begin_section(ctx, s3) is True
        gap = await sections.save_section_gap(
            ctx, s3, sections.GapDraft("required_input_missing", missing_roles=("pm",))
        )
        assert gap.state == "gap" and "GenerationGapReported" in await event_types(db)

        await documents.record_checkpoint(
            ctx, s1, "act-1", [("retrieve", 12, 4), ("draft", 800, 1)]
        )
        cp = await one(db, "SELECT state FROM graph_checkpoints WHERE section_id = $1", s1)
        assert (
            "section text" not in cp["state"] and "retrieve" in cp["state"]
        )  # names and counts only

        coh = await documents.record_coherence(ctx, "consistent", [])
        assert coh.verdict == "consistent"
        minted = await documents.mint_document_version(ctx)
        assert isinstance(minted, MintedVersion) and minted.version_no == 1
        assert (
            minted.evidence_tier == "INSUFFICIENT" and minted.completeness == "partial"
        )  # failed + gap count
        assert (
            await documents.mint_document_version(ctx)
        ).version_id == minted.version_id  # a repeat

        assert await runs.complete_run(ctx, minted) is True
        assert await runs.complete_run(ctx, minted) is False  # exactly one terminal event (WEV-5)
        assert (
            await runs.fail_run(ctx, "internal") is False
        )  # terminal: nothing written, nothing published
    done = await one(db, "SELECT status FROM generation_runs WHERE id = $1", run_id)
    art_row = await one(db, "SELECT status, current_version_id FROM artefacts WHERE id = $1", art)
    assert done["status"] == "complete" and art_row["status"] == "complete"
    assert str(art_row["current_version_id"]) == minted.version_id
    types = await event_types(db)
    assert types.count("GenerationCompleted") == 1 and "GenerationFailed" not in types
    conn = await db.admin()
    try:
        ev = await conn.fetchrow(
            "SELECT payload FROM outbox_events WHERE event_type = 'GenerationCompleted'"
        )
    finally:
        await conn.close()
    body = __import__("json").loads(ev["payload"])
    assert (body["data"].get("sections_failed"), body["data"].get("sections_gap")) == (1, 1)


async def test_a_run_without_a_gate_writes_the_sections_only(db: Db) -> None:
    s, _ = services(db)
    winput, run_id, _ = await start_run(db, gated=False)
    async with scoped(s, winput) as ctx:
        saved = await sections.save_outline(ctx, OUTLINE, gated=False)
        assert len(saved.section_ids) == 3 and saved.gate_expires_at is None
        assert (
            await sections.save_outline(ctx, OUTLINE, gated=False)
        ).section_ids == saved.section_ids
        row = await one(
            db, "SELECT status, outline_state FROM generation_runs WHERE id = $1", run_id
        )
        assert tuple(row) == ("pending", "none")  # no run edge, no outline state, no event
        assert "OutlineReady" not in await event_types(db)
        assert (
            await runs.begin_stage(ctx, "stage1") is True
        )  # no outline wait: this stage opens the run
    row = await one(db, "SELECT status, current_stage FROM generation_runs WHERE id = $1", run_id)
    assert tuple(row) == ("generating", "stage1")


async def test_a_revision_replaces_unstarted_sections_and_a_worker_cannot_delete_a_started_one(
    db: Db,
) -> None:
    s, _ = services(db)
    winput, run_id, art = await start_run(db, gated=True)
    async with scoped(s, winput) as ctx:
        await runs.begin_stage(ctx, "outline")
        first = await sections.save_outline(ctx, OUTLINE, gated=True)
        conn = await db.admin()
        try:  # the API revised the outline (W-4): revising, version + 1
            await conn.execute(
                "UPDATE generation_runs SET status = 'outline_building', outline_state = 'revising', outline_version = 2, current_stage = 'outline' WHERE id = $1",
                run_id,
            )
        finally:
            await conn.close()
        assert await runs.begin_stage(ctx, "outline") is True  # revising -> building
        revised = await sections.save_outline(
            ctx, (OutlineEntry("1", "summary"), OutlineEntry("2", "scope")), gated=True
        )
        assert len(revised.section_ids) == 2 and set(revised.section_ids).isdisjoint(
            first.section_ids
        )
        n = await one(db, "SELECT count(*) AS n FROM artefact_sections WHERE artefact_id = $1", art)
        assert n["n"] == 2
        conn = await db.admin()
        try:  # a started section cannot be deleted by a member of rag_activity
            await conn.execute(
                "UPDATE artefact_sections SET state = 'generating' WHERE id = $1",
                UUID(revised.section_ids[0]),
            )
        finally:
            await conn.close()
        with pytest.raises(asyncpg.CheckViolationError), as_tenant(A):
            await db.activity_tx.run_in_tx(
                lambda c: c.execute(
                    "DELETE FROM artefact_sections WHERE id = $1", UUID(revised.section_ids[0])
                )
            )


async def test_a_missing_row_is_run_gone_and_a_terminal_run_takes_no_write(db: Db) -> None:
    s, _ = services(db)
    winput, run_id, art = await start_run(db, gated=True, status="cancelled")
    async with scoped(s, winput) as ctx:
        assert await runs.begin_stage(ctx, "outline") is False
        with pytest.raises(RunTerminal):
            await sections.save_outline(ctx, OUTLINE, gated=True)
        assert await runs.suspend_for_quota(ctx) is False
        assert await runs.fail_run(ctx, "internal") is False
        with pytest.raises(RunTerminal):
            await documents.mint_document_version(ctx)
    conn = await db.admin()
    try:  # a purge erases the run: the next write is RunGone, never a failure to record
        await conn.execute("DELETE FROM generation_runs WHERE id = $1", run_id)
    finally:
        await conn.close()
    async with scoped(s, winput) as ctx:
        for call in (
            runs.run_state(ctx),
            runs.begin_stage(ctx, "outline"),
            runs.fail_run(ctx, "internal"),
        ):
            with pytest.raises(RunGone):
                await call


async def test_quota_suspension_is_stamped_and_a_failed_regeneration_returns_to_complete(
    db: Db,
) -> None:
    s, _ = services(db)
    winput, run_id, art = await start_run(db, gated=False)
    async with scoped(s, winput) as ctx:
        await runs.begin_stage(ctx, "stage1")
        assert await runs.suspend_for_quota(ctx) is True
        assert await runs.suspend_for_quota(ctx) is False  # a repeat
        row = await one(
            db,
            "SELECT status, failure_reason, suspended_at FROM generation_runs WHERE id = $1",
            run_id,
        )
        assert (row["status"], row["failure_reason"]) == ("suspended", "quota_exhausted") and row[
            "suspended_at"
        ] is not None
        state = await runs.run_state(ctx)
        assert state.status == "suspended" and state.suspended_at is not None
    rwin, rrun, rart = await start_run(db, gated=False, origin="regeneration")
    conn = await db.admin()
    try:  # an A-5 artefact always has a current version
        await conn.execute("SET session_replication_role = replica")
        await conn.execute(
            "UPDATE artefacts SET current_version_id = $2 WHERE id = $1", rart, uuid4()
        )
    finally:
        await conn.close()
    async with scoped(s, rwin) as ctx:
        await runs.begin_stage(ctx, "stage1")  # complete -> generating for an A-5 run
        assert (await one(db, "SELECT status FROM artefacts WHERE id = $1", rart))[
            "status"
        ] == "generating"
        assert await runs.fail_run(ctx, "inference_unavailable") is True
    assert (await one(db, "SELECT status FROM artefacts WHERE id = $1", rart))[
        "status"
    ] != "generating"


async def test_a_document_with_no_section_version_has_nothing_to_mint(db: Db) -> None:
    s, _ = services(db)
    winput, run_id, _ = await start_run(db, gated=False)
    async with scoped(s, winput) as ctx:
        await sections.save_outline(ctx, OUTLINE, gated=False)
        await runs.begin_stage(ctx, "stage1")
        with pytest.raises(NothingToMint):  # LW-5: the workflow fails the run instead
            await documents.mint_document_version(ctx)


@pytest.mark.parametrize("status", ["pending", "outline_building", "suspended"])
async def test_fail_run_ends_a_run_from_pending_outline_building_and_suspended(
    db: Db, status: str
) -> None:
    """ADR-0003 gap 5: the 30-day suspension timeout and an early failure are recorded, not lost."""
    s, _ = services(db)
    winput, run_id, art = await start_run(db, gated=True, status=status)
    if status == "suspended":  # a real suspension carries its stamp
        conn = await db.admin()
        try:
            await conn.execute(
                "UPDATE generation_runs SET suspended_at = now() WHERE id = $1", run_id
            )
        finally:
            await conn.close()
    async with scoped(s, winput) as ctx:
        assert await runs.fail_run(ctx, "internal") is True
        assert await runs.fail_run(ctx, "internal") is False  # one terminal event
    row = await one(db, "SELECT status, failure_reason FROM generation_runs WHERE id = $1", run_id)
    assert (row["status"], row["failure_reason"]) == ("failed", "internal")
    assert (await event_types(db)).count("GenerationFailed") == 1
    # the artefact never stays `pending` (the orphan sweep would restart it) or `generating`
    assert (await one(db, "SELECT status FROM artefacts WHERE id = $1", art))["status"] == "failed"
    if status == "suspended":  # LLD §4.3.6: NULL outside `suspended`
        stamped = await one(db, "SELECT suspended_at FROM generation_runs WHERE id = $1", run_id)
        assert stamped["suspended_at"] is None


async def test_load_section_text_reads_the_current_version_and_fails_closed(db: Db) -> None:
    """ADR-0003 gap 3: text in memory only, digest-checked, tenant-prefixed, one artefact only."""
    s, store = services(db)
    winput, run_id, art = await start_run(db, gated=False)
    async with scoped(s, winput) as ctx:
        await runs.begin_stage(ctx, "outline")
        saved = await sections.save_outline(ctx, OUTLINE, gated=False)
        s1, s2, _ = (UUID(i) for i in saved.section_ids)
        conn = await db.admin()
        try:
            rset = await seed.retrieval_set(conn, A, TENDER, art)
        finally:
            await conn.close()
        await sections.begin_section(ctx, s1)
        await sections.save_section(ctx, s1, draft(rset))
        assert await sections.load_section_text(ctx, s1) == "section text"
        assert await sections.load_section_text(ctx, s2) == ""  # no version yet
        with pytest.raises(RunGone):  # a section that is not this run's artefact
            await sections.load_section_text(ctx, uuid4())
        (key,) = store.objects
        store.objects[key] = b"tampered"  # ASV-4: bytes no longer hash to the stored digest
        with pytest.raises(ContentUnavailable):
            await sections.load_section_text(ctx, s1)
        del store.objects[key]  # the object is gone
        with pytest.raises(ContentUnavailable):
            await sections.load_section_text(ctx, s1)
