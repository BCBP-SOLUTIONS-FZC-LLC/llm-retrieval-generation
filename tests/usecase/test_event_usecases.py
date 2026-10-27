"""Use cases behind the consumers, with the ordering the LLD fixes (§13.4, ART-7, ART-9, LD-70)."""

from __future__ import annotations

from uuid import uuid4

import pytest
from tests.support_corpus import NOW, TENANT, TENDER
from tests.support_events import SERVICE_ACCOUNT, Bench, FakeLedger, FakeRuns, envelope
from tests.support_generation import FakeTx, make_artefact

from rag.application.events import contracts as c
from rag.application.events.consumers import (
    ArchiveTenderArtefacts,
    ForgedEvent,
    ResumeSuspendedRuns,
)
from rag.application.events.ports import SuspendedRun
from rag.application.generation.commands import AppendAuditEntry
from rag.domain.shared.enums import ArtefactArchiveState, CollectionStatus
from rag.domain.shared.errors import DomainError


async def test_the_bump_commits_before_anything_is_written_to_llm_generation() -> None:
    """Fail-safe order: a crash after TX-1 leaves a cold cache and a missing marker, never the
    reverse."""
    bench = Bench()
    col = bench.world.add_collection(status=CollectionStatus.ACTIVE)
    order: list[str] = []
    original = bench.retrieval.purge_for_document

    async def spy(*a: object, **kw: object) -> int:
        order.append(f"markers at epoch {bench.world.state.collections[col.id].purge_epoch}")
        return await original(*a, **kw)  # type: ignore[arg-type]

    bench.retrieval.purge_for_document = spy  # type: ignore[method-assign]
    ledger = FakeLedger()
    env = envelope(
        c.DOCUMENT_PURGED,
        {"document_id": str(uuid4()), "document_version": 1, "collection_id": str(col.id)},
    )

    async def once(fn):  # type: ignore[no-untyped-def]
        await ledger.process(env, fn)

    await bench.services.document_purge.execute(
        tenant_id=TENANT,
        collection_id=col.id,
        document_id=uuid4(),
        document_version=1,
        event_id=uuid4(),
        once=once,
    )
    assert order == ["markers at epoch 1"]


async def test_the_reference_platform_collection_is_not_the_tenants() -> None:
    """RLS lets every tenant *read* the PLATFORM row; EVT-14's tenant check must still refuse it."""
    from rag.domain.corpus.collection import PLATFORM

    bench = Bench()
    ref = bench.world.add_collection(status=CollectionStatus.ACTIVE, tenant_id=PLATFORM)
    with pytest.raises(ForgedEvent):
        await bench.services.document_purge.execute(
            tenant_id=TENANT,
            collection_id=ref.id,
            document_id=uuid4(),
            document_version=1,
            event_id=uuid4(),
            once=FakeLedger().process,  # type: ignore[arg-type]
        )
    assert bench.world.state.collections[ref.id].purge_epoch == 0


async def test_archiving_twice_writes_one_chain_entry_per_artefact() -> None:
    """AC-9: ``<event>:<artefact>`` is the idempotency key; the second call finds nothing live."""
    from tests.support_events import FakeArchiveRepo
    from tests.support_generation import FakeAuditRepo

    mine = make_artefact(tender_id=TENDER)
    chain = FakeAuditRepo()
    uc = ArchiveTenderArtefacts(
        FakeTx(),
        FakeArchiveRepo([mine]),
        AppendAuditEntry(chain),
        SERVICE_ACCOUNT,
        clock=lambda: NOW,
    )
    event = uuid4()
    assert await uc.execute(tenant_id=TENANT, tender_id=TENDER, event_id=event) == 1
    assert await uc.execute(tenant_id=TENANT, tender_id=TENDER, event_id=event) == 0
    assert mine.archive_state is ArtefactArchiveState.ARCHIVED and mine.archived_at == NOW
    assert (
        len(chain.entries) == 1 and chain.entries[0].draft.source_event_id == f"{event}:{mine.id}"
    )


async def test_tender_purge_moves_the_collection_before_the_artefacts_go() -> None:
    bench = Bench()
    col = bench.world.add_collection(status=CollectionStatus.ACTIVE)
    bench.artefacts.append(make_artefact(tender_id=TENDER))
    await bench.services.tender_purge.execute(tenant_id=TENANT, tender_id=TENDER, event_id=uuid4())
    assert bench.world.state.collections[col.id].status is CollectionStatus.PURGING
    # the row stays (COL-13) and the audit chain keeps its entry (AC-6)
    assert col.id in bench.world.state.collections and len(bench.chain.entries) == 1
    # a repeat finds nothing to move or delete
    await bench.services.tender_purge.execute(tenant_id=TENANT, tender_id=TENDER, event_id=uuid4())
    assert len(bench.chain.entries) == 1


async def test_quota_resume_pauses_between_batches_not_after_the_last() -> None:
    pauses: list[float] = []

    async def sleep(seconds: float) -> None:
        pauses.append(seconds)

    runs = FakeRuns([SuspendedRun(uuid4(), uuid4(), uuid4()) for _ in range(5)])
    uc = ResumeSuspendedRuns(FakeTx(), runs, runs, batch_size=2, pause_seconds=3.0, sleep=sleep)
    assert await uc.execute(resource="tokens") == 5
    assert pauses == [3.0, 3.0]  # after the 1st and 2nd full batches only


async def test_quota_resume_refuses_while_no_workflow_client_is_wired() -> None:
    runs = FakeRuns([SuspendedRun(uuid4(), uuid4(), uuid4())], available=False)
    with pytest.raises(DomainError):
        await ResumeSuspendedRuns(FakeTx(), runs, runs, 2).execute(resource="tokens")
    assert runs.resumed == []
