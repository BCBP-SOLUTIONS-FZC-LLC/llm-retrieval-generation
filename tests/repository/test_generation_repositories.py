# ruff: noqa: E501 (SQL literals)
"""The asyncpg repositories and the ART-9 / EVT-1 use cases against real Postgres."""

from __future__ import annotations

from uuid import UUID, uuid4

import asyncpg
import pytest
from tests.repository import seed
from tests.repository.support import Db, as_tenant
from tests.support_generation import FakeStore, FixedClock

from rag.application.generation.commands import (
    AppendAuditEntry,
    PurgeArtefacts,
    RecordDocumentPurge,
)
from rag.domain.generation import audit
from rag.domain.shared.enums import ArtefactStatus, ClaimOutcome, EvidenceTier, SectionState
from rag.infrastructure.persistence.generation.repositories import (
    PgArtefactPurgeRepository,
    PgArtefactReadRepository,
    PgAuditChainRepository,
    PgRetrievalSetRepository,
    PgVersionRepository,
)

pytestmark = pytest.mark.integration

A = UUID("0a3f0000-0000-7000-8000-00000000000a")
B = UUID("0a3f0000-0000-7000-8000-00000000000b")
TENDER_1 = UUID("0a3f0000-0000-7000-8000-0000000000a1")
TENDER_2 = UUID("0a3f0000-0000-7000-8000-0000000000a2")
SERVICE_ACCOUNT = UUID("0a3f0000-0000-7000-8000-0000000000ff")

COUNTED = [
    "artefacts", "artefact_versions", "artefact_version_sections", "artefact_sections",
    "artefact_section_versions", "generation_runs", "retrieval_sets", "retrieval_set_chunks",
    "retrieval_set_purges", "groundedness_reports", "claim_verifications", "completeness_checks",
    "document_facts", "fact_conflicts", "graph_checkpoints", "query_refinements",
    "instruction_refinements",
]  # fmt: skip


async def seeded(db: Db, tenant: UUID, tender: UUID | None, **kw: object) -> seed.Graph:
    async def work(c: asyncpg.Connection) -> seed.Graph:
        g = await seed.graph(c, tenant, tender, **kw)  # type: ignore[arg-type]
        await seed.facts_and_refinements(c, g)
        return g

    with as_tenant(tenant):
        g = await db.app_tx.run_in_tx(work)
        await db.activity_tx.run_in_tx(lambda c: seed.checkpoint(c, g))
    return g


async def row_counts(db: Db, tenant: UUID) -> dict[str, int]:
    async def work(c: asyncpg.Connection) -> dict[str, int]:
        return {t: await c.fetchval(f"SELECT count(*) FROM {t}") for t in COUNTED}

    with as_tenant(tenant):
        return await db.app_tx.run_in_tx(work, read_only=True)


# --- reads --------------------------------------------------------------------------------


async def test_read_repository_round_trips_the_seeded_graph(db: Db) -> None:
    g = await seeded(db, A, TENDER_1)
    repo = PgArtefactReadRepository()
    with as_tenant(A):
        view = await db.app_tx.run_in_tx(lambda c: repo.get_artefact_view(c, g.artefact))
        versions, total = await db.app_tx.run_in_tx(
            lambda c: repo.list_versions(c, g.artefact, limit=10, offset=0)
        )
        detail = await db.app_tx.run_in_tx(lambda c: repo.get_version_detail(c, g.artefact, 1))
        sections, n_sections = await db.app_tx.run_in_tx(
            lambda c: repo.list_sections(c, g.artefact, limit=10, offset=0)
        )
        record = await db.app_tx.run_in_tx(
            lambda c: repo.get_section_version_record(c, g.artefact, "sec-1", 1)
        )
        by_id = await db.app_tx.run_in_tx(
            lambda c: repo.get_section_version_record(c, g.artefact, str(g.section), 1)
        )
        missing = await db.app_tx.run_in_tx(
            lambda c: repo.get_section_version_record(c, g.artefact, "sec-1", 2)
        )
    assert view and view.artefact.status is ArtefactStatus.COMPLETE
    assert view.current_version and view.current_version.id == g.version
    assert view.current_run_id == g.run
    assert (len(versions), total, n_sections) == (1, 1, 1)
    assert detail and len(detail.sections) == 1
    d = detail.sections[0]
    assert d.groundedness and d.groundedness.id == g.report
    assert [(c.claim_ordinal, c.outcome) for c in d.claims] == [
        (1, ClaimOutcome.SUPPORTED),
        (2, ClaimOutcome.MISCITED),
    ]
    assert d.claims[1].corrected_chunk_ids == ("b:1",)
    assert d.completeness and d.completeness.missed_items == ("retention",)
    assert sections[0].section.state is SectionState.COMPLETE
    assert sections[0].evidence_tier is EvidenceTier.PARTIAL
    assert record and record.section_version.id == g.section_version
    assert by_id and by_id.section_version.id == g.section_version
    assert missing is None


async def test_reads_are_tenant_scoped_by_rls(db: Db) -> None:
    """Possessing another tenant's id yields nothing at all."""
    g = await seeded(db, A, TENDER_1)
    repo, sets = PgArtefactReadRepository(), PgRetrievalSetRepository()
    with as_tenant(B):
        assert await db.app_tx.run_in_tx(lambda c: repo.get_artefact(c, g.artefact)) is None
        assert await db.app_tx.run_in_tx(lambda c: repo.get_artefact_view(c, g.artefact)) is None
        assert await db.app_tx.run_in_tx(lambda c: sets.get_view(c, g.retrieval_set)) is None
        versions, total = await db.app_tx.run_in_tx(
            lambda c: repo.list_versions(c, g.artefact, limit=10, offset=0)
        )
    assert (versions, total) == ([], 0)


async def test_list_versions_pages_newest_first(db: Db) -> None:
    g = await seeded(db, A, TENDER_1)
    repo = PgArtefactReadRepository()

    async def add_versions(c: asyncpg.Connection) -> None:
        for n in (2, 3):
            await c.execute(
                "INSERT INTO artefact_versions (artefact_id, tenant_id, version_no, minted_reason, "
                "evidence_tier, completeness, eligibility_state, requested_mode, effective_mode, "
                "domain_profile_version, language_pack_version, model_profile_id, prompt_hash, "
                "parser_version, chunker_version, generation_run_id, delivery, origin) VALUES "
                "($1, $2, $3, 'explicit', 'SUPPORTED', 'complete', 'GENERATABLE', 'assistance', "
                "'assistance', 'dp-1', 'lp-1', 'm', 'h', 'p', 'c', $4, 'delivered', 'regeneration')",
                g.artefact,
                A,
                n,
                g.run,
            )

    with as_tenant(A):
        await db.app_tx.run_in_tx(add_versions)
        page, total = await db.app_tx.run_in_tx(
            lambda c: repo.list_versions(c, g.artefact, limit=2, offset=1)
        )
    assert (total, [v.version_no for v in page]) == (3, [2, 1])


async def test_retrieval_view_has_ordered_chunks_and_purge_rows(db: Db) -> None:
    g = await seeded(db, A, TENDER_1)
    sets = PgRetrievalSetRepository()
    with as_tenant(A):
        view = await db.app_tx.run_in_tx(lambda c: sets.get_view(c, g.retrieval_set))
    assert view and [c.rank for c in view.chunks] == [1, 2]
    assert view.purges == () and view.collection_ids == (g.collection,)
    assert view.chunks[0].chunk_id == f"{g.document}:1"  # text, not uuid (RSP-3)


# --- EVT-1 --------------------------------------------------------------------------------


async def test_document_purge_inserts_one_marker_per_set_and_is_idempotent(db: Db) -> None:
    """EVT-1, RSP-1: match on (tenant, collection, document, version); redelivery adds nothing."""
    hit = await seeded(db, A, TENDER_1)
    other_tender = await seeded(db, A, TENDER_2)  # distinct collection and document
    sets = PgRetrievalSetRepository()
    uc = RecordDocumentPurge(db.app_tx, sets)
    event = uuid4()
    args = dict(
        tenant_id=A,
        collection_id=hit.collection,
        document_id=hit.document,
        document_version=1,
        event_id=event,
    )
    with as_tenant(A):
        assert await uc.execute(**args) == 1  # type: ignore[arg-type]
        assert await uc.execute(**args) == 0  # type: ignore[arg-type]
        assert await uc.execute(**{**args, "document_version": 2, "event_id": uuid4()}) == 0  # type: ignore[arg-type]
        assert await uc.execute(**{**args, "collection_id": uuid4(), "event_id": uuid4()}) == 0  # type: ignore[arg-type]
        view = await db.app_tx.run_in_tx(lambda c: sets.get_view(c, hit.retrieval_set))
        untouched = await db.app_tx.run_in_tx(
            lambda c: sets.get_view(c, other_tender.retrieval_set)
        )
    assert view and len(view.purges) == 1
    assert view.purges[0].chunk_ids == (f"{hit.document}:1", f"{hit.document}:2")
    assert view.purges[0].event_id == event
    assert view.retrieval_set.verdict.value == "sufficient"  # RS-1: the set itself is untouched
    assert untouched and untouched.purges == ()


# --- AV-6 ---------------------------------------------------------------------------------


async def test_mark_versions_superseded_flags_only_versions_that_include_the_section(
    db: Db,
) -> None:
    g = await seeded(db, A, TENDER_1)
    other = await seeded(db, A, TENDER_2)
    repo = PgVersionRepository()
    with as_tenant(A):
        first = await db.app_tx.run_in_tx(
            lambda c: repo.mark_superseded_by_section_edit(c, g.artefact, g.section)
        )
        again = await db.app_tx.run_in_tx(
            lambda c: repo.mark_superseded_by_section_edit(c, g.artefact, g.section)
        )
        wrong_artefact = await db.app_tx.run_in_tx(
            lambda c: repo.mark_superseded_by_section_edit(c, other.artefact, g.section)
        )
    assert (first, again, wrong_artefact) == ([g.version], [], [])


# --- ART-9 --------------------------------------------------------------------------------


async def test_tender_purge_deletes_the_tenders_tree_and_nothing_else(db: Db) -> None:
    """ART-9: one tender's artefacts and every row hanging from them; the chain is kept."""
    doomed = await seeded(db, A, TENDER_1)
    survivor = await seeded(db, A, TENDER_2)
    foreign = await seeded(db, B, TENDER_1)  # same tender id, other tenant: untouched
    # an R-1 set carries the tender's id and no artefact (RS-4)

    async def r1_set(c: asyncpg.Connection) -> UUID:
        return await seed.retrieval_set(c, A, TENDER_1, None)

    with as_tenant(A):
        with_r1 = await db.app_tx.run_in_tx(r1_set)
        before = await row_counts(db, A)
    store = FakeStore()
    uc = PurgeArtefacts(
        db.app_tx,
        PgArtefactPurgeRepository(),
        AppendAuditEntry(PgAuditChainRepository()),
        store,
        FixedClock(),
    )
    with as_tenant(A):
        result = await uc.execute(
            tenant_id=A,
            tender_id=TENDER_1,
            event_id=uuid4(),
            service_account_id=SERVICE_ACCOUNT,
            reason="tender_purged",
        )
        after = await row_counts(db, A)
        chain = await db.app_tx.run_in_tx(
            lambda c: PgAuditChainRepository().list_chain(c, A), read_only=True
        )
        r1_left = await db.app_tx.run_in_tx(
            lambda c: c.fetchval("SELECT count(*) FROM retrieval_sets WHERE id = $1", with_r1)
        )
        survivor_ok = await db.app_tx.run_in_tx(
            lambda c: c.fetchval("SELECT count(*) FROM artefacts WHERE id = $1", survivor.artefact)
        )
    assert result.artefact_ids == (doomed.artefact,) and not result.s3_failed
    assert store.deleted == [(A, doomed.artefact)]
    # two identical graphs (plus the R-1 set) before; only the survivor's graph after
    expected = {t: before[t] // 2 for t in COUNTED}
    expected["retrieval_sets"] = 1
    assert after == expected
    assert (
        after["artefacts"] == 1
        and after["retrieval_set_chunks"] == 2
        and after["generation_runs"] == 1
    )
    assert r1_left == 0 and survivor_ok == 1
    # the chain kept growing: one purge entry, valid hashes, no deletion (AC-1, AC-6)
    assert [e.draft.entry_type for e in chain] == [audit.ARTEFACT_PURGED[0]]
    assert chain[0].draft.artefact_id == doomed.artefact and audit.first_break(chain) is None
    with as_tenant(B):
        assert (await row_counts(db, B))["artefacts"] == 1 and foreign.artefact


async def test_tenant_purge_deletes_free_standing_answers_too(db: Db) -> None:
    """ART-8 / EVT-15: every artefact of the tenant, whatever its tender; other tenants untouched."""
    await seeded(db, A, TENDER_1)
    await seeded(db, A, None, kind="answer")
    await seeded(db, B, TENDER_1)
    store = FakeStore()
    uc = PurgeArtefacts(
        db.app_tx,
        PgArtefactPurgeRepository(),
        AppendAuditEntry(PgAuditChainRepository()),
        store,
        FixedClock(),
    )
    with as_tenant(A):
        result = await uc.execute(
            tenant_id=A,
            tender_id=None,
            event_id=uuid4(),
            service_account_id=SERVICE_ACCOUNT,
            reason="tenant_purged",
        )
        after = await row_counts(db, A)
        chain = await db.app_tx.run_in_tx(
            lambda c: PgAuditChainRepository().list_chain(c, A), read_only=True
        )
    assert len(result.artefact_ids) == 2 and set(after.values()) == {0}
    assert store.deleted == [(A, None)]
    assert [e.seq for e in chain] == [1, 2] and audit.first_break(chain) is None
    with as_tenant(B):
        assert (await row_counts(db, B))["artefacts"] == 1


async def test_purge_is_atomic_when_the_delete_fails(db: Db) -> None:
    """The audit entries and the delete share one transaction: a failure leaves both untouched."""

    class Failing(PgArtefactPurgeRepository):
        async def delete_artefact_trees(self, tx, tenant_id, tender_id, artefact_ids) -> None:  # type: ignore[no-untyped-def]
            await super().delete_artefact_trees(tx, tenant_id, tender_id, artefact_ids)
            raise RuntimeError("boom")

    g = await seeded(db, A, TENDER_1)
    store = FakeStore()
    uc = PurgeArtefacts(
        db.app_tx, Failing(), AppendAuditEntry(PgAuditChainRepository()), store, FixedClock()
    )
    with as_tenant(A):
        with pytest.raises(RuntimeError):
            await uc.execute(
                tenant_id=A,
                tender_id=TENDER_1,
                event_id=uuid4(),
                service_account_id=SERVICE_ACCOUNT,
                reason="r",
            )
        counts = await row_counts(db, A)
        chain = await db.app_tx.run_in_tx(
            lambda c: PgAuditChainRepository().list_chain(c, A), read_only=True
        )
    assert counts["artefacts"] == 1 and chain == [] and store.deleted == [] and g.artefact
