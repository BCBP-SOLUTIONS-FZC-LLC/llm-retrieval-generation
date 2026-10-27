# ruff: noqa: E501 (SQL literals)
"""The workflows SQL against real Postgres: guarded gate transitions, atomic cancel, A-6/A-7 writes."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from typing import Any
from uuid import UUID, uuid4

import asyncpg
import pytest
from tests.repository import seed
from tests.repository.support import Db, as_tenant
from tests.repository.test_generation_writes import verification
from tests.support_generate import Store
from tests.support_generation import FakeScope, FixedClock
from tests.support_retrieval import FakePacks, bundle, identity, scope, section_profile
from tests.support_workflows import FakeWorkflows

from rag.application.generation.commands import AppendAuditEntry, MarkVersionsSuperseded
from rag.application.generation.flow import (
    AdmissionRequest,
    Admitted,
    CompleteGeneration,
    CompletionInput,
    RunLifecycle,
    SectionRegeneration,
)
from rag.application.retrieval.scope import ScopeSpec
from rag.application.workflows.common import RunActions
from rag.application.workflows.signals import (
    ApproveOutline,
    CancelGeneration,
    OutlineEdit,
    ReviseOutline,
)
from rag.application.workflows.state import GetGenerationState
from rag.domain.generation.artefact import Artefact
from rag.domain.generation.eligibility import resolve_eligibility
from rag.domain.generation.facts import DocumentFact
from rag.domain.generation.repositories import IdempotencyRace
from rag.domain.generation.run import GenerationRun
from rag.domain.generation.section import ArtefactSection
from rag.domain.packs.generation import EligibilityDecl
from rag.domain.shared.enums import (
    ArtefactKind,
    ArtefactOrigin,
    ConflictResolution,
    DeliveryState,
    EligibilityState,
    GenerationMode,
    GrantLevel,
    RunStatus,
)
from rag.domain.shared.errors import DomainError, ErrorCode
from rag.domain.shared.identity import RequestIdentity
from rag.infrastructure.events.publisher import OutboxEventPublisher
from rag.infrastructure.persistence.generation.regeneration import (
    PgFactRepository,
    PgInstructionRefinementRepository,
    PgRegenerationRepository,
)
from rag.infrastructure.persistence.generation.repositories import (
    PgAuditChainRepository,
    PgVersionRepository,
)
from rag.infrastructure.persistence.generation.workflows import PgRunRepository
from rag.infrastructure.persistence.generation.writes import PgGenerationWriteRepository

pytestmark = pytest.mark.integration

A = UUID("0a3f0000-0000-7000-8000-000000000001")
B = UUID("0a3f0000-0000-7000-8000-00000000000b")
PRINCIPAL = UUID("0a3f0000-0000-7000-8000-000000000002")
TENDER = UUID("0a3f0000-0000-7000-8000-0000000000a1")
runs, writes = PgRunRepository(), PgGenerationWriteRepository()
facts = PgFactRepository()


async def sql(db: Db, tenant: UUID, fn: Any, *, read_only: bool = False) -> Any:
    with as_tenant(tenant):
        return await db.app_tx.run_in_tx(fn, read_only=read_only)


async def long_form(
    db: Db,
    tenant: UUID = A,
    *,
    status: str = "awaiting_outline_approval",
    outline: str = "ready",
    version: int | None = 1,
    sections: tuple[str, ...] = ("complete", "generating", "pending"),
    workflow_id: str | None = None,
    artefact_status: str = "generating",
) -> tuple[UUID, UUID, list[UUID]]:
    """A document artefact, its async run in ``status`` and one section row per state."""
    art = Artefact(id=uuid4(), tenant_id=tenant, tender_id=TENDER, kind=ArtefactKind.DOCUMENT,
                   capability="technical_proposal", created_by=PRINCIPAL)  # fmt: skip
    run = GenerationRun(
        id=uuid4(), artefact_id=art.id, tenant_id=tenant, principal_id=PRINCIPAL,
        requested_mode=GenerationMode.ASSISTANCE, effective_mode=GenerationMode.ASSISTANCE,
        eligibility_state=EligibilityState.GENERATABLE,
        domain_profile_version="dp-1", language_pack_version="lp-1", tender_id=TENDER,
        workflow_id=workflow_id or str(art.id),
        run_input={"plan": "standard", "residency_mode": "standard", "kind": "document"},
    )  # fmt: skip
    ids: list[UUID] = []

    async def work(c: asyncpg.Connection) -> None:
        await writes.insert_shell(c, art, run, None)
        await c.execute(
            "UPDATE generation_runs SET status = $2::run_status, outline_state = $3::outline_state, "
            "outline_version = $4, current_stage = 'outline' WHERE id = $1",
            run.id, status, outline, version if outline != "none" else None,
        )  # fmt: skip
        await c.execute(
            "UPDATE artefacts SET status = $2::artefact_status WHERE id = $1",
            art.id,
            artefact_status,
        )
        for i, state in enumerate(sections, start=1):
            sid: UUID = await c.fetchval(
                "INSERT INTO artefact_sections (artefact_id, tenant_id, tender_section_ref, section_type, ordinal, state) "
                "VALUES ($1, $2, $3, 'introduction', $4, $5::section_state) RETURNING id",
                art.id, tenant, f"sec-{i}", i, "pending" if state == "complete" else state,
            )  # fmt: skip
            ids.append(sid)
            if state == "complete":
                rs = await seed.retrieval_set(c, tenant, TENDER, art.id)
                sv: UUID = await c.fetchval(
                    "INSERT INTO artefact_section_versions (section_id, tenant_id, version_no, content_ref, content_sha256, "
                    "retrieval_set_id, evidence_tier, completeness, model_profile_id, prompt_hash, serving_tier) "
                    "VALUES ($1, $2, 1, 'k', '\\x" + "00" * 32 + "'::bytea, $3, 'PARTIAL', 'partial', 'm', 'h', 'primary') RETURNING id",
                    sid, tenant, rs,
                )  # fmt: skip
                await c.execute(
                    "UPDATE artefact_sections SET state = 'generating' WHERE id = $1", sid
                )
                await c.execute(
                    "UPDATE artefact_sections SET state = 'complete', current_version_id = $2 WHERE id = $1",
                    sid,
                    sv,
                )

    await sql(db, tenant, work)
    return art.id, run.id, ids


def actions(
    db: Db, *, scope: FakeScope | None = None, wf: FakeWorkflows | None = None, audit: Any = None
) -> tuple[RunActions, FakeWorkflows]:
    wf = wf or FakeWorkflows()
    return (
        RunActions(
            tx=db.app_tx, runs=runs, scope=scope or FakeScope(), packs=FakePacks(bundle()),
            workflows=wf, events=OutboxEventPublisher(),
            audit_log=AppendAuditEntry(audit or PgAuditChainRepository()), clock=FixedClock(),
        ),
        wf,
    )  # fmt: skip


async def row(db: Db, table: str, id_: UUID, tenant: UUID = A) -> Any:
    return await sql(
        db,
        tenant,
        lambda c: c.fetchrow(f"SELECT * FROM {table} WHERE id = $1", id_),
        read_only=True,
    )


async def count(db: Db, table: str, where: str = "true", tenant: UUID = A) -> int:
    return int(
        await sql(
            db,
            tenant,
            lambda c: c.fetchval(f"SELECT count(*) FROM {table} WHERE {where}"),
            read_only=True,
        )
    )


# --- RUN-2: one run row per workflow id ---------------------------------------------------------
async def test_workflow_id_is_unique_within_a_tenant_while_set(db: Db) -> None:
    art_id, run_id, _ = await long_form(db, workflow_id="regen:abc")
    base = await sql(db, A, lambda c: runs.get_run(c, run_id), read_only=True)
    clone = replace(base, id=uuid4())
    with pytest.raises(IdempotencyRace):  # a repeated A-5 can never insert a second run row
        await sql(db, A, lambda c: writes.insert_run(c, clone))
    assert await count(db, "generation_runs") == 1
    # a synchronous run has no workflow id and never clashes
    sync1, sync2 = (
        replace(base, id=uuid4(), workflow_id=None),
        replace(base, id=uuid4(), workflow_id=None),
    )
    await sql(db, A, lambda c: writes.insert_run(c, sync1))
    await sql(db, A, lambda c: writes.insert_run(c, sync2))
    found = await sql(db, A, lambda c: runs.find_run_by_workflow_id(c, "regen:abc"), read_only=True)
    assert found is not None and found.id == run_id and art_id
    assert (
        await sql(db, B, lambda c: runs.find_run_by_workflow_id(c, "regen:abc"), read_only=True)
        is None
    )  # RLS


# --- the guarded gate UPDATE ------------------------------------------------------------------------
async def test_save_gate_is_one_guarded_update_on_status_and_record_version(db: Db) -> None:
    _, run_id, _ = await long_form(db)
    run = await sql(db, A, lambda c: runs.get_run(c, run_id), read_only=True)
    after = replace(run)
    after.approve_outline()
    stale = await sql(
        db,
        A,
        lambda c: runs.save_gate(
            c,
            after,
            expected_status=RunStatus.AWAITING_OUTLINE_APPROVAL,
            expected_record_version=run.record_version + 5,
        ),
    )
    wrong = await sql(
        db,
        A,
        lambda c: runs.save_gate(
            c,
            after,
            expected_status=RunStatus.SUSPENDED,
            expected_record_version=run.record_version,
        ),
    )
    assert stale is None and wrong is None  # zero rows: the caller answers 409
    assert (await row(db, "generation_runs", run_id))["status"] == "awaiting_outline_approval"
    seq = await sql(
        db,
        A,
        lambda c: runs.save_gate(
            c,
            after,
            expected_status=RunStatus.AWAITING_OUTLINE_APPROVAL,
            expected_record_version=run.record_version,
        ),
    )
    assert seq == run.record_version + 1  # the trigger bumped it: progress_seq (EVT-13)
    got = await row(db, "generation_runs", run_id)
    assert (got["status"], got["outline_state"], got["current_stage"]) == (
        "generating",
        "approved",
        "stage1",
    )
    assert (
        await sql(
            db,
            A,
            lambda c: runs.save_gate(
                c,
                after,
                expected_status=RunStatus.AWAITING_OUTLINE_APPROVAL,
                expected_record_version=run.record_version,
            ),
        )
        is None
    )  # a repeat


async def test_concurrent_approvals_transition_once_and_publish_once(db: Db) -> None:
    _, run_id, _ = await long_form(db)
    a, wf = actions(db)
    approve = ApproveOutline(a)

    async def go() -> str:
        with as_tenant(A):
            try:
                await approve.execute(identity(), run_id, 1)
                return "202"
            except DomainError as exc:
                return exc.code.value

    results = await asyncio.gather(go(), go())
    assert set(results) <= {"202", "WORKFLOW_STATE_CONFLICT"} and "202" in results
    assert (await row(db, "generation_runs", run_id))["status"] == "generating"
    assert await count(db, "outbox_events", "event_type = 'GenerationProgressed'") == 1  # not two
    assert len(wf.signals) >= 1


async def test_revise_increments_outline_version_in_sql_and_ck_run_outline_holds(db: Db) -> None:
    _, run_id, _ = await long_form(db, version=2)
    a, wf = actions(db)
    ack = await _with(A, ReviseOutline(a).execute(identity(), run_id, OutlineEdit(2)))
    got = await row(db, "generation_runs", run_id)
    assert (got["status"], got["outline_state"], got["outline_version"]) == (
        "outline_building",
        "revising",
        3,
    )
    assert ack.outline_version == 3 and wf.signals[0][1] == "revise_outline"


async def _with(tenant: UUID, coro: Any) -> Any:
    with as_tenant(tenant):
        return await coro


# --- W-5: atomic cancel -------------------------------------------------------------------------------
async def test_cancel_is_one_transaction_run_artefact_sections_audit_and_event(db: Db) -> None:
    art_id, run_id, ids = await long_form(
        db, status="generating", outline="approved", sections=("complete", "generating", "pending")
    )
    a, wf = actions(db)
    await _with(A, CancelGeneration(a).execute(identity(), run_id, "superseded"))
    assert (await row(db, "generation_runs", run_id))["status"] == "cancelled"
    assert (await row(db, "artefacts", art_id))["status"] == "cancelled"  # ART-6
    states = [(await row(db, "artefact_sections", s))["state"] for s in ids]
    assert states == ["complete", "failed", "pending"]  # AS-8
    assert (await row(db, "artefact_sections", ids[1]))[
        "failure_reason"
    ] is not None  # ck_as_failed
    assert await count(db, "artefact_audit_chain", "entry_type = 'llm.generation.cancelled'") == 1
    assert await count(db, "outbox_events", "event_type = 'GenerationCancelled'") == 1
    assert wf.signals == [(str(art_id), "cancel", {"reason": "superseded"})]  # after the commit


async def test_cancel_failing_in_its_last_statement_leaves_everything_as_it_was(db: Db) -> None:
    art_id, run_id, ids = await long_form(db, status="generating", outline="approved")

    class Boom(PgAuditChainRepository):
        async def append(self, tx: Any, draft: Any) -> Any:
            raise RuntimeError("chain unavailable")

    a, wf = actions(db, audit=Boom())
    with pytest.raises(RuntimeError):
        await _with(A, CancelGeneration(a).execute(identity(), run_id, "x"))
    assert (await row(db, "generation_runs", run_id))["status"] == "generating"
    assert (await row(db, "artefacts", art_id))["status"] == "generating"
    assert [(await row(db, "artefact_sections", s))["state"] for s in ids] == [
        "complete",
        "generating",
        "pending",
    ]
    assert await count(db, "outbox_events") == 0 and await count(db, "artefact_audit_chain") == 0
    assert wf.signals == []


async def test_a_regeneration_runs_cancel_leaves_a_complete_artefact_complete(db: Db) -> None:
    art_id, run_id, _ = await long_form(
        db, status="pending", outline="none", artefact_status="complete", sections=("complete",)
    )
    a, _ = actions(db)
    await _with(A, CancelGeneration(a).execute(identity(), run_id, None))
    assert (await row(db, "generation_runs", run_id))["status"] == "cancelled"
    assert (await row(db, "artefacts", art_id))["status"] == "complete"


async def test_scope_revoked_ends_the_run_in_one_transaction(db: Db) -> None:
    art_id, run_id, ids = await long_form(db, status="generating", outline="approved")
    a, wf = actions(db, scope=FakeScope(DomainError(ErrorCode.COLLECTION_NOT_FOUND)))
    with pytest.raises(DomainError) as exc:
        await _with(A, ApproveOutline(a).execute(identity(), run_id, 1))
    assert exc.value.code is ErrorCode.ARTEFACT_NOT_FOUND
    got = await row(db, "generation_runs", run_id)
    assert (got["status"], got["failure_reason"], got["failed_at_stage"]) == (
        "failed",
        "scope_revoked",
        "outline",
    )
    assert (await row(db, "artefacts", art_id))["status"] == "failed"
    assert [(await row(db, "artefact_sections", s))["state"] for s in ids] == [
        "complete",
        "failed",
        "pending",
    ]
    assert await count(db, "outbox_events", "event_type = 'GenerationFailed'") == 1
    assert await count(db, "artefact_audit_chain", "entry_type = 'llm.generation.failed'") == 1
    assert wf.signals[0][1] == "cancel"


# --- W-2 ---------------------------------------------------------------------------------------------
async def test_w2_reads_the_composite_from_our_tables_under_rls(db: Db) -> None:
    art_id, run_id, ids = await long_form(db)
    await sql(db, A, lambda c: c.execute(
        "INSERT INTO document_facts (artefact_id, tenant_id, fact_key, value, established_by_section_id) "
        "VALUES ($1, $2, 'access_method', 'crane', $3)", art_id, A, ids[0]))  # fmt: skip
    state = await _with(
        A, GetGenerationState(db.app_tx, runs, FakeScope()).execute(identity(), run_id)
    )
    assert (state.status, state.outline.version, state.progress.total, state.progress.complete) == (
        "awaiting_outline_approval", 1, 3, 1)  # fmt: skip
    assert state.established_facts == {"access_method": "crane"}
    assert state.sections[0].evidence_tier == "PARTIAL" and state.sections[0].section_version_id
    with pytest.raises(DomainError) as exc:  # another tenant cannot see the run
        await _with(
            B,
            GetGenerationState(db.app_tx, runs, FakeScope()).execute(
                RequestIdentity(B, PRINCIPAL), run_id
            ),
        )
    assert exc.value.code is ErrorCode.ARTEFACT_NOT_FOUND


# --- A-7: facts, conflicts, flags ---------------------------------------------------------------------
async def graph(db: Db) -> seed.Graph:
    return await sql(db, A, lambda c: seed.graph(c, A, TENDER))


async def test_fact_resolution_sql_supersede_insert_flag_and_the_guard(db: Db) -> None:
    g = await graph(db)
    conflict_id = await sql(db, A, lambda c: seed.facts_and_refinements(c, g))
    other = await sql(db, A, lambda c: c.fetchval(
        "INSERT INTO artefact_sections (artefact_id, tenant_id, tender_section_ref, section_type, ordinal, current_version_id) "
        "VALUES ($1, $2, 'sec-2', 'risk', 2, $3) RETURNING id", g.artefact, A, g.section_version))  # fmt: skip
    from datetime import UTC, datetime

    now = datetime(2026, 4, 2, 9, tzinfo=UTC)
    conflict = await sql(db, A, lambda c: facts.get_conflict(c, conflict_id, lock=True))
    assert conflict is not None and conflict.resolution is None
    conflict.resolution, conflict.resolved_at = ConflictResolution.UPDATE_FACT, now
    assert await sql(db, A, lambda c: facts.save_resolution(c, conflict)) is True
    again = replace(conflict, resolution=ConflictResolution.DISMISS)
    assert (
        await sql(db, A, lambda c: facts.save_resolution(c, again)) is False
    )  # WHERE resolution IS NULL
    # uq_fact_current: the new fact can only follow the supersession of the old one
    new = DocumentFact(uuid4(), g.artefact, A, "access_method", "hoist", g.section)
    with pytest.raises(asyncpg.UniqueViolationError):
        await sql(db, A, lambda c: facts.insert_fact(c, new))

    async def apply(c: asyncpg.Connection) -> list[UUID]:
        assert await facts.supersede_fact(c, g.artefact, "access_method", now)
        await facts.insert_fact(c, new)
        return await facts.flag_sections(
            c, g.artefact, except_section_id=g.section, reason="fact_updated:access_method"
        )

    flagged = await sql(db, A, apply)
    assert flagged == [other]  # only sections with a version, never the edited one
    current = await sql(db, A, lambda c: facts.current_facts(c, g.artefact), read_only=True)
    assert [(f.fact_key, f.value) for f in current] == [("access_method", "hoist")]
    assert (await row(db, "artefact_sections", other))[
        "flagged_reason"
    ] == "fact_updated:access_method"
    # DF-1: a fact's value is never overwritten, only its supersession marker moves
    with pytest.raises(asyncpg.PostgresError):
        await sql(
            db,
            A,
            lambda c: c.execute(
                "UPDATE document_facts SET value = 'x' WHERE artefact_id = $1", g.artefact
            ),
        )
    # the dataset row learns the resolution
    await sql(
        db,
        A,
        lambda c: PgInstructionRefinementRepository().record_resolution(
            c, conflict_id, "update_fact"
        ),
    )
    ir = await sql(
        db,
        A,
        lambda c: c.fetchrow(
            "SELECT conflict_resolution FROM instruction_refinements WHERE fact_conflict_id = $1",
            conflict_id,
        ),
        read_only=True,
    )
    assert ir["conflict_resolution"] == "update_fact"


async def test_another_tenant_sees_neither_facts_nor_conflicts(db: Db) -> None:
    g = await graph(db)
    conflict_id = await sql(db, A, lambda c: seed.facts_and_refinements(c, g))
    assert await sql(db, B, lambda c: facts.get_conflict(c, conflict_id), read_only=True) is None
    assert await sql(db, B, lambda c: facts.current_facts(c, g.artefact), read_only=True) == []


# --- A-6: a section version and no document version ------------------------------------------------
async def test_section_regeneration_completion_writes_one_section_version_and_marks_documents(
    db: Db,
) -> None:
    g = await graph(db)
    audit = AppendAuditEntry(PgAuditChainRepository())
    store, clock = Store(), FixedClock()
    complete = CompleteGeneration(db.app_tx, writes, store, audit, OutboxEventPublisher(), clock,
                                  MarkVersionsSuperseded(PgVersionRepository()))  # fmt: skip
    lifecycle = RunLifecycle(db.app_tx, writes, audit, clock)
    regen = PgRegenerationRepository()
    art, sec_pair = await sql(db, A, lambda c: _entities(c, g), read_only=True)
    run = GenerationRun(
        id=uuid4(), artefact_id=g.artefact, tenant_id=A, principal_id=PRINCIPAL,
        requested_mode=GenerationMode.ASSISTANCE, effective_mode=GenerationMode.ASSISTANCE,
        eligibility_state=EligibilityState.GENERATABLE,
        domain_profile_version="dp-1", language_pack_version="lp-1", tender_id=TENDER,
        origin=ArtefactOrigin.REGENERATION, base_version_id=g.version,
        run_input={"synchronous": True},
    )  # fmt: skip
    await sql(db, A, lambda c: writes.insert_run(c, run))
    elig = resolve_eligibility(EligibilityDecl(), GenerationMode.ASSISTANCE, "dp-1")
    adm = Admitted(
        identity=identity(), request=AdmissionRequest(ArtefactKind.DOCUMENT, "technical_proposal", "introduction", "q", ScopeSpec(TENDER), GrantLevel.GENERATE),
        scope=scope(), bundle=bundle(), pack_markers=(), eligibility=elig,
        profile=section_profile(), artefact=art, run=run, section=sec_pair,
    )  # fmt: skip
    rs = await sql(db, A, lambda c: seed.retrieval_set(c, A, TENDER, g.artefact))
    prior_version_no = 1
    with as_tenant(A):
        await lifecycle.start(adm)
        assert (
            adm.artefact.status.value == "complete"
        )  # a section regeneration leaves the artefact at rest
        done = await complete.execute_regeneration(
            CompletionInput(admitted=adm, retrieval_set_id=rs, content=b"New text [c-01].", prompt_hash="sha256:bb",
                            model_profile_id="gen-large", serving_tier="primary", verification=verification(),
                            delivery=DeliveryState.DELIVERED),
            SectionRegeneration(g.section_version, 1),
        )  # fmt: skip
    assert (done.section_version.version_no, done.section_version.origin) == (
        prior_version_no + 1,
        ArtefactOrigin.REGENERATION,
    )
    assert done.superseded_version_ids == (g.version,)
    assert await count(db, "artefact_versions") == 1  # REGEN-8: no document version minted
    assert (await row(db, "artefact_versions", g.version))[
        "superseded_by_section_edit"
    ] is True  # AV-6
    art_row = await row(db, "artefacts", g.artefact)
    assert (art_row["status"], art_row["current_version_id"]) == ("complete", g.version)
    sec_row = await row(db, "artefact_sections", g.section)
    assert (sec_row["state"], sec_row["current_version_id"]) == (
        "complete",
        done.section_version.id,
    )
    sv_row = await row(db, "artefact_section_versions", done.section_version.id)
    assert (sv_row["origin"], sv_row["manual_regen_count"], sv_row["version_no"]) == (
        "regeneration",
        1,
        2,
    )
    assert (
        await count(db, "groundedness_reports") == 2 and await count(db, "completeness_checks") == 2
    )
    ev = await sql(
        db, A, lambda c: c.fetchrow("SELECT event_type, payload FROM outbox_events"), read_only=True
    )
    assert ev["event_type"] == "SectionCompleted" and str(g.version) in str(ev["payload"])  # LD-31
    assert await count(db, "artefact_audit_chain", "entry_type = 'llm.section.regenerated'") == 1
    old = await sql(db, A, lambda c: regen.get_section(c, g.artefact, "sec-1"), read_only=True)
    assert old is not None and old[1] is not None and old[1].id == done.section_version.id
    assert await sql(
        db,
        A,
        lambda c: regen.document_holds_section_version(c, g.version, g.section_version),
        read_only=True,
    )
    assert not await sql(
        db,
        A,
        lambda c: regen.document_holds_section_version(c, g.version, done.section_version.id),
        read_only=True,
    )


async def _entities(c: asyncpg.Connection, g: seed.Graph) -> tuple[Artefact, ArtefactSection]:
    from rag.infrastructure.persistence.generation import rows

    a = rows.artefact(await c.fetchrow("SELECT * FROM artefacts WHERE id = $1", g.artefact))
    s = rows.section(await c.fetchrow("SELECT * FROM artefact_sections WHERE id = $1", g.section))
    return a, s
