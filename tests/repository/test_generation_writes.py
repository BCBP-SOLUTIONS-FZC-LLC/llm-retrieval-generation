# ruff: noqa: E501 (SQL literals)
"""Admission and completion SQL against real Postgres: RUN-3, GEN-10, §14.1, RLS, triggers."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from typing import Any
from uuid import UUID, uuid4

import asyncpg
import pytest
from tests.repository import seed
from tests.repository.support import Db, as_tenant
from tests.support_generate import Events, Store
from tests.support_generation import FixedClock
from tests.support_retrieval import bundle, scope, section_profile

from rag.application.generation.commands import AppendAuditEntry
from rag.application.generation.flow import (
    AdmissionRequest,
    Admitted,
    CompleteGeneration,
    CompletionInput,
    RunLifecycle,
)
from rag.application.retrieval.scope import ScopeSpec
from rag.application.verification.verify import ClaimResult, VerificationResult
from rag.domain.generation.artefact import Artefact
from rag.domain.generation.eligibility import resolve_eligibility
from rag.domain.generation.repositories import CompletionRows, IdempotencyRace
from rag.domain.generation.run import GenerationRun
from rag.domain.generation.section import ArtefactSection
from rag.domain.packs.generation import EligibilityDecl
from rag.domain.shared.enums import (
    ArtefactKind,
    ArtefactStatus,
    ClaimOutcome,
    CompletenessVerdict,
    DeliveryState,
    EvidenceTier,
    GenerationMode,
    GrantLevel,
    RunStatus,
    SectionState,
)
from rag.domain.verification.completeness import Completeness
from rag.domain.verification.scoring import Disposition
from rag.infrastructure.events.publisher import OutboxEventPublisher
from rag.infrastructure.persistence.generation.probes import PgStoreProbe
from rag.infrastructure.persistence.generation.repositories import PgAuditChainRepository
from rag.infrastructure.persistence.generation.writes import PgGenerationWriteRepository

pytestmark = pytest.mark.integration

A = UUID("0a3f0000-0000-7000-8000-000000000001")  # the tenant tests.support_retrieval uses
B = UUID("0a3f0000-0000-7000-8000-00000000000b")
PRINCIPAL = UUID("0a3f0000-0000-7000-8000-000000000002")
repo = PgGenerationWriteRepository()


def entities(
    tenant: UUID = A, key: str | None = None
) -> tuple[Artefact, GenerationRun, ArtefactSection]:
    art = Artefact(id=uuid4(), tenant_id=tenant, tender_id=None, kind=ArtefactKind.ANSWER,
                   capability="answer", created_by=PRINCIPAL)  # fmt: skip
    run = GenerationRun(
        id=uuid4(), artefact_id=art.id, tenant_id=tenant, principal_id=PRINCIPAL,
        requested_mode=GenerationMode.AUTOMATION, effective_mode=GenerationMode.ASSISTANCE,
        eligibility_state=resolve_eligibility(
            EligibilityDecl(), GenerationMode.ASSISTANCE, "dp-1"
        ).state,
        domain_profile_version="dp-1", language_pack_version="lp-1", clamped=True,
        clamp_reason="profile_withholds_unattended_completion", idempotency_key=key,
        request_hash="h1" if key else None, run_input={"synchronous": True},
    )  # fmt: skip
    sec = ArtefactSection(id=uuid4(), artefact_id=art.id, tenant_id=tenant,
                          section_type="answer", ordinal=1)  # fmt: skip
    return art, run, sec


async def admit(
    db: Db, tenant: UUID = A, key: str | None = None
) -> tuple[Artefact, GenerationRun, ArtefactSection]:
    art, run, sec = entities(tenant, key)
    with as_tenant(tenant):
        await db.app_tx.run_in_tx(lambda c: repo.insert_shell(c, art, run, sec))
    return art, run, sec


def admitted(art: Artefact, run: GenerationRun, sec: ArtefactSection) -> Admitted:
    elig = resolve_eligibility(EligibilityDecl(), GenerationMode.ASSISTANCE, "dp-1")
    b = bundle()
    return Admitted(
        identity=__import__("tests.support_retrieval", fromlist=["identity"]).identity(),
        request=AdmissionRequest(
            ArtefactKind.ANSWER, "answer", "answer", "q", ScopeSpec(), GrantLevel.ANSWER
        ),
        scope=scope(), bundle=b, pack_markers=(), eligibility=elig,
        profile=section_profile(), artefact=art, run=run, section=sec,
    )  # fmt: skip


def verification(**kw: Any) -> VerificationResult:
    base: dict[str, Any] = dict(
        claims=(
            ClaimResult(1, ClaimOutcome.SUPPORTED, ("v1:1",), (), 1),
            ClaimResult(2, ClaimOutcome.MISCITED, ("v1:1",), ("v2:2",), 2),
        ),
        supported=1, unsupported=0, miscited=1, gaps=0, tiers_run=(1, 2), score=__import__("decimal").Decimal("1.000"),
        tier=EvidenceTier.SUPPORTED,
        completeness=Completeness(CompletenessVerdict.PARTIAL, 5, 4, ("retention",)),
        disposition=Disposition.SURFACE_FLAGGED, judge_model_profile_id="judge-small",
    )  # fmt: skip
    return VerificationResult(**{**base, **kw})


def completion_service(
    db: Db, events: Events | None = None
) -> tuple[CompleteGeneration, RunLifecycle, Store]:
    audit = AppendAuditEntry(PgAuditChainRepository())
    store = Store()
    clock = FixedClock()
    return (
        CompleteGeneration(db.app_tx, repo, store, audit, events or OutboxEventPublisher(), clock),
        RunLifecycle(db.app_tx, repo, audit, clock),
        store,
    )


async def counts(db: Db, tenant: UUID = A) -> dict[str, int]:
    tables = ["artefacts", "generation_runs", "artefact_sections", "artefact_versions",
              "artefact_section_versions", "artefact_version_sections", "groundedness_reports",
              "claim_verifications", "completeness_checks", "artefact_audit_chain", "outbox_events"]  # fmt: skip

    async def work(c: asyncpg.Connection) -> dict[str, int]:
        return {t: await c.fetchval(f"SELECT count(*) FROM {t}") for t in tables}

    with as_tenant(tenant):
        return await db.app_tx.run_in_tx(work, read_only=True)


# --- admission (RUN-1, RUN-3, LD-3) ------------------------------------------------------------


async def test_admission_commits_shell_run_and_section_with_the_clamp_and_pinned_versions(
    db: Db,
) -> None:
    art, run, _ = await admit(db, key="k1")

    async def read(c: asyncpg.Connection) -> Any:
        return await c.fetchrow("SELECT * FROM generation_runs WHERE id = $1", run.id)

    with as_tenant(A):
        row = await db.app_tx.run_in_tx(read, read_only=True)
        found = await db.app_tx.run_in_tx(lambda c: repo.find_run_by_key(c, A, PRINCIPAL, "k1"))
    assert row["status"] == "pending" and row["clamped"] and row["clamp_reason"]
    assert (row["domain_profile_version"], row["language_pack_version"]) == ("dp-1", "lp-1")
    assert (row["requested_mode"], row["effective_mode"]) == ("automation", "assistance")
    assert found is not None and found.id == run.id and found.request_hash == "h1"
    assert found.created_at is not None and found.status is RunStatus.PENDING
    assert (await counts(db))["artefact_sections"] == 1 and art.id


async def test_a_lost_idempotency_race_rolls_the_whole_shell_back(db: Db) -> None:
    await admit(db, key="dup")
    art, run, sec = entities(A, "dup")  # a second admission with the same key
    with as_tenant(A), pytest.raises(IdempotencyRace):
        await db.app_tx.run_in_tx(lambda c: repo.insert_shell(c, art, run, sec))
    c = await counts(db)
    assert (c["artefacts"], c["generation_runs"], c["artefact_sections"]) == (1, 1, 1)  # no orphan


async def test_the_key_is_unique_per_tenant_and_principal_and_released_for_a_retry(db: Db) -> None:
    first, run, _ = await admit(db, key="k")
    await admit(db, tenant=B, key="k")  # another tenant: no clash (RUN-3 is per tenant+principal)
    with as_tenant(A):
        await db.app_tx.run_in_tx(lambda c: repo.release_key(c, run.id))
        assert (
            await db.app_tx.run_in_tx(lambda c: repo.find_run_by_key(c, A, PRINCIPAL, "k")) is None
        )
    await admit(db, key="k")  # reusable once released
    assert first.id


async def test_another_tenant_cannot_see_the_run_by_key(db: Db) -> None:
    await admit(db, key="secret")
    with as_tenant(B):
        assert (
            await db.app_tx.run_in_tx(lambda c: repo.find_run_by_key(c, B, PRINCIPAL, "secret"))
            is None
        )
        assert (
            await db.app_tx.run_in_tx(lambda c: repo.find_run_by_key(c, A, PRINCIPAL, "secret"))
            is None
        )


async def test_save_state_applies_the_machines_and_the_section_trigger_guards_them(db: Db) -> None:
    art, run, sec = await admit(db)
    art.move_to(ArtefactStatus.GENERATING)
    run.move_to(RunStatus.GENERATING)
    sec.start()
    with as_tenant(A):
        await db.app_tx.run_in_tx(lambda c: repo.save_state(c, art, run, sec))
        bad = replace(sec, state=SectionState.PENDING)  # generating -> pending is no edge (AS-5)
        with pytest.raises(asyncpg.PostgresError):
            await db.app_tx.run_in_tx(lambda c: repo.save_state(c, art, run, bad))


# --- completion (GEN-10, ASV-4, AC-8, §14.1) ---------------------------------------------------


async def run_completion(db: Db, events: Events | None = None):  # type: ignore[no-untyped-def]
    art, run, sec = await admit(db)
    svc, lifecycle, store = completion_service(db, events)
    adm = admitted(art, run, sec)
    with as_tenant(A):
        rs = await db.app_tx.run_in_tx(lambda c: seed.retrieval_set(c, A, None, art.id))
        await lifecycle.start(adm)
        done = await svc.execute(
            CompletionInput(
                admitted=adm, retrieval_set_id=rs, content=b"The access route is gate 4 [c-01].",
                prompt_hash="sha256:aa", model_profile_id="gen-large", serving_tier="primary",
                verification=verification(), delivery=DeliveryState.DISCONNECTED,
                disclaimer_ref="disc-1",
            )
        )  # fmt: skip
    return art, run, sec, done, store


async def test_completion_writes_everything_in_one_transaction(db: Db) -> None:
    art, run, sec, done, store = await run_completion(db)
    c = await counts(db)
    assert (
        c["artefact_versions"] == c["artefact_section_versions"] == c["groundedness_reports"] == 1
    )
    assert (c["claim_verifications"], c["completeness_checks"], c["artefact_version_sections"]) == (
        2,
        1,
        1,
    )
    assert (
        c["artefact_audit_chain"] == 1 and c["outbox_events"] == 1
    )  # GEN-10: event with the version

    async def read(conn: asyncpg.Connection) -> Any:
        return (
            await conn.fetchrow(
                "SELECT status, current_version_id FROM artefacts WHERE id=$1", art.id
            ),
            await conn.fetchrow("SELECT status FROM generation_runs WHERE id=$1", run.id),
            await conn.fetchrow(
                "SELECT state, current_version_id FROM artefact_sections WHERE id=$1", sec.id
            ),
            await conn.fetchrow("SELECT * FROM artefact_versions WHERE id=$1", done.version.id),
            await conn.fetchrow(
                "SELECT * FROM artefact_section_versions WHERE id=$1", done.section_version.id
            ),
            await conn.fetchrow("SELECT event_type, tenant_id FROM outbox_events"),
        )

    with as_tenant(A):
        a_row, r_row, s_row, v_row, sv_row, ev = await db.app_tx.run_in_tx(read, read_only=True)
    assert (a_row["status"], a_row["current_version_id"]) == ("complete", done.version.id)
    assert r_row["status"] == "complete" and s_row["state"] == "complete"
    assert s_row["current_version_id"] == done.section_version.id
    assert (v_row["delivery"], v_row["disclaimer_ref"], v_row["version_no"]) == (
        "disconnected",
        "disc-1",
        1,
    )
    assert (v_row["clamped"], v_row["generation_run_id"]) == (False, run.id)
    assert bytes(sv_row["content_sha256"]) == done.section_version.content_sha256
    assert store.objects[sv_row["content_ref"]] == b"The access route is gate 4 [c-01]."
    assert (ev["event_type"], ev["tenant_id"]) == ("GenerationCompleted", str(A))


async def test_a_failing_event_rolls_the_version_back_gen_10(db: Db) -> None:
    class Boom(Events):
        async def publish(self, tx: Any, event: Any) -> None:
            await super().publish(tx, event)
            raise RuntimeError("outbox down")

    art, run, sec = await admit(db)
    svc, lifecycle, _ = completion_service(db, Boom())
    adm = admitted(art, run, sec)
    with as_tenant(A):
        rs = await db.app_tx.run_in_tx(lambda c: seed.retrieval_set(c, A, None, art.id))
        await lifecycle.start(adm)
        with pytest.raises(Exception, match="could not be recorded"):
            await svc.execute(
                CompletionInput(admitted=adm, retrieval_set_id=rs, content=b"x", prompt_hash="h",
                                model_profile_id="m", serving_tier="p", verification=verification(),
                                delivery=DeliveryState.DELIVERED)
            )  # fmt: skip
    c = await counts(db)
    assert (c["artefact_versions"], c["artefact_section_versions"], c["groundedness_reports"]) == (
        0,
        0,
        0,
    )
    assert c["artefact_audit_chain"] == 0 and c["outbox_events"] == 0
    assert (
        adm.artefact.status is ArtefactStatus.GENERATING
    )  # the in-memory machines did not advance


async def test_version_numbers_are_gapless_under_concurrent_writers(db: Db) -> None:
    """§14.1: the per-artefact lock makes two regenerations two versions, never a collision."""
    art, run, sec, done, _ = await run_completion(db)

    async def mint() -> int:
        async def work(c: asyncpg.Connection) -> int:
            vno, sno = await repo.next_version_numbers(c, art.id, sec.id)
            await asyncio.sleep(0.05)  # the second writer must wait on the lock, not collide
            v = replace(done.version, id=uuid4(), version_no=vno)
            sv = replace(
                done.section_version, id=uuid4(), version_no=sno,
                content_ref=done.section_version.content_ref + f".{vno}",
            )  # fmt: skip
            await repo.insert_completion(c, CompletionRows(v, sv, None, (), None))
            return vno

        with as_tenant(A):
            return await db.app_tx.run_in_tx(work)

    got = await asyncio.gather(mint(), mint())
    assert sorted(got) == [2, 3]


async def test_the_store_probe_passes_on_a_writable_primary(db: Db) -> None:
    with as_tenant(A):
        await PgStoreProbe(db.app_tx).require_writable()
