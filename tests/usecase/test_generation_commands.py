"""AppendAuditEntry, MarkVersionsSuperseded, ART-9 purge and EVT-1 purge markers (fakes)."""

from __future__ import annotations

from dataclasses import replace
from uuid import UUID, uuid4

from tests.support_generation import (
    NOW,
    TENANT,
    TENDER,
    FakeAuditRepo,
    FakePurgeRepo,
    FakeRetrievalRepo,
    FakeStore,
    FakeTx,
    FixedClock,
    make_artefact,
    make_retrieval_view,
)

from rag.application.generation.commands import (
    AppendAuditEntry,
    PurgeArtefacts,
    RecordDocumentPurge,
)
from rag.domain.generation import audit
from rag.domain.shared.enums import ArtefactKind, AuditActorType

SERVICE_ACCOUNT = UUID("0a3f0000-0000-7000-8000-0000000000ff")


def purge_uc(
    artefacts: list, store: FakeStore | None = None
) -> tuple[PurgeArtefacts, FakePurgeRepo, FakeAuditRepo, FakeStore]:
    repo, chain, store = FakePurgeRepo(artefacts), FakeAuditRepo(), store or FakeStore()
    uc = PurgeArtefacts(FakeTx(), repo, AppendAuditEntry(chain), store, FixedClock())
    return uc, repo, chain, store


async def test_tender_purge_audits_each_artefact_then_deletes_then_clears_s3() -> None:
    a, b = make_artefact(), make_artefact()
    other_tender = make_artefact(tender_id=uuid4())
    uc, repo, chain, store = purge_uc([a, b, other_tender])
    event = uuid4()
    result = await uc.execute(
        tenant_id=TENANT,
        tender_id=TENDER,
        event_id=event,
        service_account_id=SERVICE_ACCOUNT,
        reason="tender_purged",
    )
    assert set(result.artefact_ids) == {a.id, b.id}
    assert repo.deleted == [(TENANT, TENDER, list(result.artefact_ids))]
    assert [x.id for x in repo.artefacts] == [other_tender.id]
    # one llm.artefact.purged entry per artefact, kept after the delete (AC-1, AC-6)
    assert {e.draft.artefact_id for e in chain.entries} == {a.id, b.id}
    first = chain.entries[0].draft
    assert (first.entry_type, first.action) == audit.ARTEFACT_PURGED
    assert first.actor_type is AuditActorType.SERVICE_ACCOUNT and first.actor_id == SERVICE_ACCOUNT
    assert first.metadata == {"reason": "tender_purged"} and first.occurred_at == NOW
    assert first.source_event_id.startswith(f"{event}:")
    assert audit.first_break(chain.entries) is None
    # S3 after commit, one prefix per artefact
    assert {x for _, x in store.deleted} == {a.id, b.id}
    assert (result.s3_deleted, result.s3_failed) == (2, False)


async def test_tenant_purge_covers_free_standing_answers_and_clears_the_tenant_prefix() -> None:
    """ART-8: a tender-less answer is deleted by the tenant purge; S3 goes by tenant prefix."""
    answer = make_artefact(tender_id=None, kind=ArtefactKind.ANSWER)
    doc = make_artefact()
    uc, repo, chain, store = purge_uc([answer, doc])
    result = await uc.execute(
        tenant_id=TENANT,
        tender_id=None,
        event_id=uuid4(),
        service_account_id=SERVICE_ACCOUNT,
        reason="tenant_purged",
    )
    assert set(result.artefact_ids) == {answer.id, doc.id}
    assert repo.deleted[0][1] is None and repo.artefacts == []
    assert len(chain.entries) == 2
    assert store.deleted == [(TENANT, None)]


async def test_s3_failure_after_commit_is_reported_not_raised() -> None:
    """The DB erasure committed; the orphan sweep removes the leftover objects."""
    uc, repo, chain, _ = purge_uc([make_artefact()], FakeStore(fail_delete=True))
    result = await uc.execute(
        tenant_id=TENANT,
        tender_id=TENDER,
        event_id=uuid4(),
        service_account_id=SERVICE_ACCOUNT,
        reason="tender_purged",
    )
    assert result.s3_failed and repo.artefacts == [] and len(chain.entries) == 1


async def test_purge_is_idempotent_for_a_redelivered_event() -> None:
    """AC-9: the same event id appends nothing a second time; a second run finds nothing."""
    a = make_artefact()
    uc, repo, chain, _ = purge_uc([a])
    kwargs = dict(
        tenant_id=TENANT,
        tender_id=TENDER,
        event_id=uuid4(),
        service_account_id=SERVICE_ACCOUNT,
        reason="tender_purged",
    )
    await uc.execute(**kwargs)  # type: ignore[arg-type]
    again = await uc.execute(**kwargs)  # type: ignore[arg-type]
    assert again.artefact_ids == () and len(chain.entries) == 1


async def test_append_audit_returns_none_on_duplicate_source_event_id() -> None:
    chain = FakeAuditRepo()
    append = AppendAuditEntry(chain)
    d = audit.AuditEntryDraft(
        tenant_id=TENANT,
        occurred_at=NOW,
        entry_type=audit.GENERATION_COMPLETED[0],
        action=audit.GENERATION_COMPLETED[1],
        actor_type=AuditActorType.USER,
        actor_id=uuid4(),
        artefact_id=uuid4(),
        source_event_type="llm.generation.completed",
        source_event_id="evt",
    )
    first = await append(None, d)
    assert first is not None and first.seq == 1 and first.prev_hash is None
    assert await append(None, d) is None
    second = await append(None, replace(d, source_event_id="evt2"))
    assert second is not None and second.seq == 2 and second.prev_hash == first.entry_hash


async def test_document_purge_marks_only_the_matching_tuple_and_is_idempotent() -> None:
    """EVT-1: (tenant, collection, document, version); EVT-6/RSP-1: a redelivery adds nothing."""
    hit, miss = make_retrieval_view(), make_retrieval_view()
    repo = FakeRetrievalRepo(hit, miss)
    uc = RecordDocumentPurge(FakeTx(), repo)
    c = hit.chunks[0]
    event = uuid4()
    args = dict(
        tenant_id=TENANT,
        collection_id=c.collection_id,
        document_id=c.document_id,
        document_version=c.document_version,
        event_id=event,
    )
    assert await uc.execute(**args) == 1  # type: ignore[arg-type]
    assert await uc.execute(**args) == 0  # type: ignore[arg-type]
    assert repo.views[hit.retrieval_set.id].purges[0].chunk_ids == (c.chunk_id,)
    assert repo.views[miss.retrieval_set.id].purges == ()
    # a different version of the same document is not purged
    assert await uc.execute(**{**args, "document_version": 2, "event_id": uuid4()}) == 0  # type: ignore[arg-type]
