"""The six SQS consumers over eventcommon's mock transport: tenant binding, idempotent
redelivery, unknown types, poison -> DLQ."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any
from uuid import UUID, uuid4

import pytest
from eventcommon.mock import MockConsumer, MockDLQPublisher
from pgcommon import guc_set_from_context
from tests.support_corpus import OTHER_TENANT, TENANT, TENDER
from tests.support_events import Bench, FakeLedger, FakeRuns, envelope, view
from tests.support_generation import make_artefact as _make_artefact
from tests.support_logs import RecordingLog

from rag.application.events import contracts as c
from rag.application.events.ports import SuspendedRun
from rag.domain.shared.enums import ArtefactArchiveState, ArtefactKind, CollectionStatus
from rag.infrastructure.events import consumers as h
from rag.infrastructure.persistence.pools import request_guc_provider


def make_artefact(**kw: Any) -> Any:
    """An artefact of the corpus tests' tender."""
    return _make_artefact(**{"tender_id": TENDER, **kw})


class Rig:
    """One consumer wired to a mock queue and a mock DLQ."""

    def __init__(
        self, bench: Bench, factory: Callable[[h.ConsumerServices, Any], h.Handler]
    ) -> None:
        self.bench = bench
        self.ledger = FakeLedger()
        self.dlq = MockDLQPublisher("dlq")
        self.queue = MockConsumer(queue_url="q")
        self.queue.set_handler(h.poison_safe(factory(bench.services, self.ledger), self.dlq))

    async def send(self, env: Any) -> None:
        await self.queue.inject(env)


def purge_payload(collection: UUID, document: UUID, version: int = 1) -> dict[str, Any]:
    return {
        "document_id": str(document),
        "document_version": version,
        "collection_id": str(collection),
    }


# --- tenant_state ---------------------------------------------------------------------------
async def test_suspension_freezes_and_a_redelivery_is_a_noop() -> None:
    bench = Bench()
    col = bench.world.add_collection(status=CollectionStatus.ACTIVE)
    rig = Rig(bench, h.tenant_state)
    env = envelope(c.TENANT_STATE_CHANGED, {"status": "suspended", "cause": "TenantSuspended"})
    await rig.send(env)
    assert bench.world.state.collections[col.id].status is CollectionStatus.FROZEN
    assert len(bench.world.state.events("CollectionStatusChanged")) == 1
    await rig.send(env)  # same envelope id: ledgered, so nothing runs
    assert len(bench.world.state.events("CollectionStatusChanged")) == 1
    assert uuid_of(env) in rig.ledger.done and rig.dlq.sent == []


def uuid_of(env: Any) -> UUID:
    return UUID(env.id)


async def test_offboarded_and_past_due_change_nothing() -> None:
    bench = Bench()
    col = bench.world.add_collection(status=CollectionStatus.ACTIVE)
    rig = Rig(bench, h.tenant_state)
    for status in ("offboarded", "past_due", "cancelled", "something_new"):
        await rig.send(envelope(c.TENANT_STATE_CHANGED, {"status": status}))
    assert bench.world.state.collections[col.id].status is CollectionStatus.ACTIVE


# --- tenant binding -------------------------------------------------------------------------
async def test_the_events_tenant_is_bound_around_the_use_case() -> None:
    """The pool provider (outside a request) must yield the validated envelope tenant."""
    seen: list[Any] = []
    bench = Bench()
    original = bench.services.apply_tenant_state

    class Spy:
        async def execute(self, tenant: UUID, status: str, cause: str | None = None) -> Any:
            seen.append((tenant, guc_set_from_context(), request_guc_provider()))
            return await original.execute(tenant, status, cause)

    from dataclasses import replace

    bench.services = replace(bench.services, apply_tenant_state=Spy())  # type: ignore[arg-type]
    rig = Rig(bench, h.tenant_state)
    await rig.send(envelope(c.TENANT_STATE_CHANGED, {"status": "active"}))
    [(tenant, ctx, provided)] = seen
    assert tenant == TENANT and ctx.tenant_id == str(TENANT) and provided == ctx
    assert request_guc_provider() is None  # and the binding does not leak past the handler


@pytest.mark.parametrize("tenant", ["system", "not-a-uuid"])
async def test_a_tenant_that_is_not_a_uuid_is_poison(tenant: str) -> None:
    """LD-19: ``'system'`` would crash every ``::uuid`` policy."""
    bench = Bench()
    rig = Rig(bench, h.tenant_state)
    await rig.send(envelope(c.TENANT_STATE_CHANGED, {"status": "suspended"}, tenant=tenant))
    assert len(rig.dlq.sent) == 1 and "uuid" in rig.dlq.sent[0].reason
    assert rig.ledger.done == set()


async def test_a_payload_tenant_that_disagrees_with_the_envelope_is_poison() -> None:
    bench = Bench()
    col = bench.world.add_collection(status=CollectionStatus.ACTIVE)
    rig = Rig(bench, h.tenant_state)
    await rig.send(
        envelope(c.TENANT_STATE_CHANGED, {"status": "suspended", "tenant_id": str(OTHER_TENANT)})
    )
    assert len(rig.dlq.sent) == 1
    assert bench.world.state.collections[col.id].status is CollectionStatus.ACTIVE


# --- unknown, malformed ---------------------------------------------------------------------
async def test_an_unknown_type_is_acked_and_logged_not_dead_lettered(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rec = RecordingLog()
    monkeypatch.setattr(h, "log", rec)
    rig = Rig(Bench(), h.tenant_state)
    await rig.send(envelope("TenantSomethingElse", {"x": 1}))
    assert rig.dlq.sent == []
    [fields] = rec.find("ignoring_event_type_on_queue", "info")
    assert fields["event_type"] == "TenantSomethingElse"


async def test_a_payload_missing_a_field_goes_straight_to_the_dlq_with_the_original_body() -> None:
    rig = Rig(Bench(), h.tenant_state)
    env = envelope(c.TENANT_STATE_CHANGED, {"cause": "no status"})
    await rig.send(env)
    [sent] = rig.dlq.sent
    assert env.id.encode() in sent.body and sent.reason.startswith("poison")
    assert rig.ledger.done == set()


async def test_a_transient_failure_propagates_so_sqs_redelivers() -> None:
    bench = Bench()
    rig = Rig(bench, h.tenant_state)

    class Down:
        async def execute(self, *a: Any) -> None:
            raise ConnectionError("llm_corpus unreachable")

    from dataclasses import replace

    bench.services = replace(bench.services, apply_tenant_state=Down())  # type: ignore[arg-type]
    rig.queue.set_handler(h.poison_safe(h.tenant_state(bench.services, rig.ledger), rig.dlq))  # type: ignore[arg-type]
    with pytest.raises(ConnectionError):
        await rig.send(envelope(c.TENANT_STATE_CHANGED, {"status": "suspended"}))
    assert rig.dlq.sent == [] and rig.ledger.done == set()


# --- tenant_offboarding ---------------------------------------------------------------------
async def test_offboarding_is_phase_one_only_and_alerts(monkeypatch: pytest.MonkeyPatch) -> None:
    """EVT-15: collections to purging, lifecycle purged; nothing deleted, nothing in S3 touched."""
    bench = Bench()
    col = bench.world.add_collection(status=CollectionStatus.ACTIVE)
    artefact = make_artefact()
    bench.artefacts.append(artefact)
    rec = RecordingLog()
    monkeypatch.setattr(h, "log", rec)
    rig = Rig(bench, h.tenant_offboarding)
    await rig.send(envelope(c.TENANT_MEMBERSHIPS_PURGED, {"actor_id": "iam"}))
    assert bench.world.state.collections[col.id].status is CollectionStatus.PURGING
    assert bench.world.state.lifecycle[TENANT].purged
    assert bench.purge_repo.artefacts == [artefact] and bench.store.deleted == []
    [alert] = rec.find("ALERT tenant_offboarding_phase_one_applied", "error")
    assert alert["tenant_id"] == str(TENANT)


# --- membership (one IAM queue, two event types) ---------------------------------------------
async def test_the_one_iam_queue_routes_each_event_type_to_its_handler() -> None:
    """CD-44: ``membership-rag-q`` carries both; the type decides, the ledger name is shared."""
    bench = Bench()
    col = bench.world.add_collection(status=CollectionStatus.ACTIVE)
    rig = Rig(bench, h.membership)
    await rig.send(envelope(c.TENANT_STATE_CHANGED, {"status": "suspended", "cause": "x"}))
    assert bench.world.state.collections[col.id].status is CollectionStatus.FROZEN
    await rig.send(envelope(c.TENANT_MEMBERSHIPS_PURGED, {"actor_id": "iam"}))
    assert bench.world.state.collections[col.id].status is CollectionStatus.PURGING
    assert bench.world.state.lifecycle[TENANT].purged and rig.dlq.sent == []


# --- tender_lifecycle -----------------------------------------------------------------------
async def test_tender_restricted_promotes_once() -> None:
    bench = Bench()
    bench.world.add_collection(status=CollectionStatus.ACTIVE)
    rig = Rig(bench, h.tender_lifecycle)
    env = envelope(c.TENDER_RESTRICTED, {"tender_id": str(TENDER)})
    await rig.send(env)
    await rig.send(env)
    assert len(bench.world.state.events("CollectionPromoted")) == 1


async def test_tender_purged_moves_the_collection_and_deletes_the_artefacts() -> None:
    bench = Bench()
    col = bench.world.add_collection(status=CollectionStatus.ACTIVE)
    mine, other = make_artefact(), make_artefact(tender_id=uuid4())
    bench.artefacts.extend([mine, other])
    rig = Rig(bench, h.tender_lifecycle)
    await rig.send(envelope(c.TENDER_PURGED, {"tender_id": str(TENDER)}))
    assert bench.world.state.collections[col.id].status is CollectionStatus.PURGING
    assert [a.id for a in bench.purge_repo.artefacts] == [other.id]
    assert [e.draft.entry_type for e in bench.chain.entries] == ["llm.artefact.purged"]
    assert bench.store.deleted == [(TENANT, mine.id)]


@pytest.mark.parametrize("outcome", sorted(c.TENDER_TERMINAL_TYPES))
async def test_a_terminal_outcome_archives_and_the_first_one_wins(outcome: str) -> None:
    """ART-7 / COL-14: closed once, artefacts archived once, a later outcome changes nothing."""
    bench = Bench()
    col = bench.world.add_collection(status=CollectionStatus.ACTIVE)
    art, free = make_artefact(), make_artefact(tender_id=None, kind=ArtefactKind.ANSWER)
    bench.artefacts.extend([art, free])
    rig = Rig(bench, h.tender_lifecycle)
    await rig.send(envelope(outcome, {"tender_id": str(TENDER)}))
    closed_at = bench.world.state.collections[col.id].tender_closed_at
    assert closed_at is not None
    assert art.archive_state is ArtefactArchiveState.ARCHIVED
    assert free.archive_state is ArtefactArchiveState.LIVE  # ART-8
    [entry] = bench.chain.entries
    assert (entry.draft.entry_type, entry.draft.artefact_id) == ("llm.artefact.archived", art.id)
    await rig.send(envelope("TenderWon", {"tender_id": str(TENDER)}))
    assert len(bench.chain.entries) == 1
    assert bench.world.state.collections[col.id].tender_closed_at == closed_at
    assert bench.artefacts == [art, free]  # archived, never deleted


async def test_other_tender_types_are_ignored() -> None:
    rig = Rig(Bench(), h.tender_lifecycle)
    await rig.send(envelope("TenderReceived", {"tender_id": str(TENDER)}))
    assert rig.dlq.sent == []


# --- purge_projection -----------------------------------------------------------------------
async def test_document_purge_bumps_the_epoch_then_marks_each_citing_set_once() -> None:
    """§13.4 / EVT-1: epoch first, markers second; the ledger row comes with the markers (PE-1)."""
    bench = Bench()
    col = bench.world.add_collection(status=CollectionStatus.ACTIVE)
    doc = uuid4()
    citing, other = view(col.id, doc), view(col.id, uuid4())
    bench.retrieval.views = {v.retrieval_set.id: v for v in (citing, other)}
    rig = Rig(bench, h.purge_projection)
    env = envelope(c.DOCUMENT_PURGED, purge_payload(col.id, doc))
    await rig.send(env)
    assert bench.world.state.collections[col.id].purge_epoch == 1
    assert bench.cache.evicted == [(TENANT, col.id)]
    [marker] = bench.retrieval.views[citing.retrieval_set.id].purges  # one row per set
    assert len(marker.chunk_ids) == 2 and marker.purged_document_id == doc
    assert bench.retrieval.views[other.retrieval_set.id].purges == ()
    assert uuid_of(env) in rig.ledger.done
    await rig.send(env)  # redelivery: the read-only ledger lookup acks it, no second bump
    assert bench.world.state.collections[col.id].purge_epoch == 1


async def test_a_tx2_failure_leaves_the_bump_and_the_redelivery_completes_the_markers() -> None:
    """PE-2: the ledger row is written in TX-2 only, so TX-1 + failed TX-2 retries safely."""
    bench = Bench()
    col = bench.world.add_collection(status=CollectionStatus.ACTIVE)
    doc = uuid4()
    v = view(col.id, doc)
    bench.retrieval.views = {v.retrieval_set.id: v}
    rig = Rig(bench, h.purge_projection)
    rig.ledger.fail_next_effect = True
    env = envelope(c.DOCUMENT_PURGED, purge_payload(col.id, doc))
    with pytest.raises(RuntimeError):
        await rig.send(env)
    assert bench.world.state.collections[col.id].purge_epoch == 1 and rig.ledger.done == set()
    await rig.send(env)
    assert rig.ledger.done == {uuid_of(env)}
    assert len({p.event_id for p in bench.retrieval.views[v.retrieval_set.id].purges}) == 1


async def test_a_forged_purge_naming_another_tenants_collection_is_refused() -> None:
    """EVT-14: under the claimed tenant that collection is not visible / not theirs."""
    bench = Bench()
    victim = bench.world.add_collection(status=CollectionStatus.ACTIVE, tenant_id=OTHER_TENANT)
    rig = Rig(bench, h.purge_projection)
    await rig.send(envelope(c.DOCUMENT_PURGED, purge_payload(victim.id, uuid4())))
    assert len(rig.dlq.sent) == 1
    assert bench.world.state.collections[victim.id].purge_epoch == 0
    assert rig.ledger.done == set()


async def test_a_purge_for_a_missing_collection_is_refused_too() -> None:
    rig = Rig(Bench(), h.purge_projection)
    await rig.send(envelope(c.DOCUMENT_PURGED, purge_payload(uuid4(), uuid4())))
    assert len(rig.dlq.sent) == 1


async def test_a_placement_cache_failure_never_fails_the_purge() -> None:
    bench = Bench()
    bench.cache.fail_evict = True
    col = bench.world.add_collection(status=CollectionStatus.ACTIVE)
    rig = Rig(bench, h.purge_projection)
    await rig.send(envelope(c.DOCUMENT_PURGED, purge_payload(col.id, uuid4())))
    assert bench.world.state.collections[col.id].purge_epoch == 1 and rig.dlq.sent == []


# --- pack_invalidation ----------------------------------------------------------------------
async def test_a_profile_publication_clears_the_cache_and_emits_one_refreshed_event() -> None:
    """EVT-4: cache only; the signal is enqueued with the ledger row, once."""
    bench = Bench()
    rig = Rig(bench, h.pack_invalidation)
    payload = {"domain": "civil", "language": "en", "concern": "sections", "version": "4.2.0"}
    env = envelope(c.DOMAIN_PROFILE_PUBLISHED, payload)
    await rig.send(env)
    await rig.send(env)
    assert bench.cache.invalidated == [(TENANT, "civil", "en", "sections")]
    [event] = bench.events.events
    assert event.type == c.PROFILE_CACHE_REFRESHED and event.tenant_id == TENANT
    assert event.payload == {
        "profile_ref": "dp-civil-en",
        "domain": "civil",
        "language": "en",
        "concern": "sections",
        "from_version": "4.1.0",
        "to_version": "4.2.0",
        "triggered_by_event_id": env.id,
    }
    from rag.infrastructure.events.schemas import validate_published

    validate_published(event.type, event.payload)


# --- quota_resume ---------------------------------------------------------------------------
def suspended(n: int) -> list[SuspendedRun]:
    return [SuspendedRun(uuid4(), uuid4(), uuid4()) for _ in range(n)]


async def test_tokens_restored_resumes_in_batches_and_a_repeat_resumes_nothing_twice() -> None:
    bench = Bench()
    bench.runs.runs = suspended(5)
    rig = Rig(bench, h.quota_resume)
    env = envelope(c.TENANT_QUOTA_RESTORED, {"resource": "tokens", "cause": "plan_upgrade"})
    await rig.send(env)
    assert sorted(bench.runs.resumed) == sorted(r.run_id for r in bench.runs.runs)
    assert bench.runs.pages == 3  # 2 + 2 + 1
    await rig.send(env)
    assert len(bench.runs.resumed) == 5


async def test_limit_raised_cause_resumes_like_any_other_cause() -> None:
    """A token add-on emits ``cause = limit_raised`` (Commercial AI-6); the cause is never read."""
    bench = Bench()
    bench.runs.runs = suspended(5)
    rig = Rig(bench, h.quota_resume)
    env = envelope(c.TENANT_QUOTA_RESTORED, {"resource": "tokens", "cause": "limit_raised"})
    await rig.send(env)
    assert sorted(bench.runs.resumed) == sorted(r.run_id for r in bench.runs.runs)
    await rig.send(env)
    assert len(bench.runs.resumed) == 5


async def test_only_the_tokens_resource_acts() -> None:
    bench = Bench()
    bench.runs.runs = suspended(3)
    rig = Rig(bench, h.quota_resume)
    await rig.send(envelope(c.TENANT_QUOTA_RESTORED, {"resource": "storage"}))
    assert bench.runs.resumed == [] and rig.ledger.done  # acked and ledgered, nothing to do


async def test_without_a_workflow_client_the_message_retries_instead_of_being_ledgered() -> None:
    bench = Bench()
    bench.runs = FakeRuns(suspended(2), available=False)
    from dataclasses import replace

    from tests.support_generation import FakeTx

    from rag.application.events.consumers import ResumeSuspendedRuns

    bench.services = replace(
        bench.services, quota_resume=ResumeSuspendedRuns(FakeTx(), bench.runs, bench.runs, 2)
    )
    rig = Rig(bench, h.quota_resume)
    from rag.domain.shared.errors import DomainError

    with pytest.raises(DomainError):
        await rig.send(envelope(c.TENANT_QUOTA_RESTORED, {"resource": "tokens"}))
    assert rig.ledger.done == set() and rig.dlq.sent == []
