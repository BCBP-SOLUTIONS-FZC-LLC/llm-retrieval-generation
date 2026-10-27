"""Tenant and tender lifecycle use cases (§12.4, §13.5, §13.8, COL-8, COL-12..14, TL-1..4)."""

from __future__ import annotations

from uuid import uuid4

from tests.support_corpus import NOW, OTHER_TENANT, TENANT, TENDER, World

from rag.application.corpus.lifecycle import (
    ApplyTenantState,
    BeginTenderPurge,
    CloseTender,
    OffboardTenant,
    PurgeTenantCorpus,
    RestrictionOutcome,
    RestrictTender,
)
from rag.domain.corpus.collection import REFERENCE, derive_collection_id
from rag.domain.corpus.tenant_lifecycle import TenantAction, TenantLifecycle, TenantState
from rag.domain.shared.enums import CollectionStatus as C
from rag.domain.shared.enums import CollectionType
from rag.domain.shared.enums import IndexGenerationStatus as G


def _tenant_state(w: World) -> ApplyTenantState:
    return ApplyTenantState(w.tx, w.repos, w.events, clock=lambda: NOW)


# --- tenant_state ---------------------------------------------------------------------------


async def test_freeze_records_interrupted_status_per_collection_and_writes_the_lifecycle_row() -> (
    None
):
    """COL-12 / TL-2: provisioning and active both freeze, each remembering its own status."""
    w = World()
    prov = w.add_collection(status=C.PROVISIONING, tender_id=uuid4())
    act = w.add_collection(status=C.ACTIVE)
    action = await _tenant_state(w).execute(TENANT, "suspended", "TenantSuspended")
    assert action is TenantAction.FREEZE
    assert (w.state.collections[prov.id].status_before_freeze) is C.PROVISIONING
    assert (w.state.collections[act.id].status_before_freeze) is C.ACTIVE
    row = w.state.lifecycle[TENANT]
    assert (row.state, row.cause) == (TenantState.FROZEN, "TenantSuspended")
    events = w.state.events("CollectionStatusChanged")
    assert {e.payload["reason"] for e in events} == {"tenant_suspended"} and len(events) == 2


async def test_restore_goes_back_to_the_recorded_status_never_blindly_to_active() -> None:
    """COL-12 / LD-44: a half-built provisioning corpus is not published by a reinstatement."""
    w = World()
    prov = w.add_collection(status=C.PROVISIONING, tender_id=uuid4())
    act = w.add_collection(status=C.ACTIVE)
    await _tenant_state(w).execute(TENANT, "trial_expired")
    assert await _tenant_state(w).execute(TENANT, "active") is TenantAction.RESTORE
    assert w.state.collections[prov.id].status is C.PROVISIONING
    assert w.state.collections[act.id].status is C.ACTIVE
    assert all(c.status_before_freeze is None for c in w.state.collections.values())
    assert TENANT not in w.state.lifecycle  # the row is deleted on reactivation


async def test_freeze_and_restore_are_idempotent_and_leave_other_tenants_alone() -> None:
    """A redelivery changes nothing; another tenant's rows are never touched."""
    w = World()
    mine = w.add_collection()
    theirs = w.add_collection(tenant_id=OTHER_TENANT)
    for _ in range(2):
        await _tenant_state(w).execute(TENANT, "suspended")
    assert w.state.collections[mine.id].status is C.FROZEN
    assert w.state.collections[theirs.id].status is C.PROVISIONING
    for _ in range(2):
        await _tenant_state(w).execute(TENANT, "trial")
    assert w.state.collections[mine.id].status is C.PROVISIONING


async def test_past_due_cancelled_and_offboarded_change_nothing() -> None:
    """LD-46: not a freeze; offboarded is the purge consumer's (EVT-15)."""
    w = World()
    c = w.add_collection()
    for status in ("past_due", "cancelled", "offboarded"):
        assert await _tenant_state(w).execute(TENANT, status) is TenantAction.IGNORE
    assert w.state.collections[c.id] == c and not w.state.lifecycle and not w.state.outbox


async def test_a_purged_tenant_is_not_restored() -> None:
    """TL-1: purged is terminal; a late reactivation is a no-op."""
    w = World()
    c = w.add_collection(status=C.PURGING)
    w.state.lifecycle[TENANT] = TenantLifecycle(TENANT, TenantState.PURGED, NOW)
    await _tenant_state(w).execute(TENANT, "active")
    await _tenant_state(w).execute(TENANT, "suspended")
    assert w.state.collections[c.id].status is C.PURGING
    assert w.state.lifecycle[TENANT].state is TenantState.PURGED


# --- tenant_offboarding ---------------------------------------------------------------------


async def test_offboarding_phase_one_moves_to_purging_and_writes_purged_in_one_transaction() -> (
    None
):
    """EVT-15 / TL-2: nothing deleted; frozen rows too; the reference corpus is spared (SCOPE-9)."""
    w = World()
    act = w.add_collection(status=C.ACTIVE)
    frozen = w.add_collection(status=C.FROZEN, tender_id=uuid4())
    ref = w.add_collection(tender_id=REFERENCE, tenant_id=OTHER_TENANT)  # stands in for PLATFORM
    await OffboardTenant(w.tx, w.repos, w.events, clock=lambda: NOW).execute(
        TENANT, "TenantOffboarded"
    )
    assert w.state.collections[act.id].status is C.PURGING
    assert w.state.collections[frozen.id].status is C.PURGING
    assert w.state.collections[frozen.id].status_before_freeze is None
    assert w.state.collections[ref.id].status is not C.PURGING
    assert w.state.lifecycle[TENANT] == TenantLifecycle(
        TENANT, TenantState.PURGED, NOW, "TenantOffboarded"
    )
    assert {e.payload["reason"] for e in w.state.events("CollectionStatusChanged")} == {
        "purge_started"
    }


async def test_offboarding_a_frozen_tenant_and_a_repeat_keep_the_first_changed_at() -> None:
    """frozen -> purged is allowed; a redelivery changes nothing."""
    w = World()
    w.add_collection()
    w.state.lifecycle[TENANT] = TenantLifecycle(TENANT, TenantState.FROZEN, NOW.replace(day=1))
    await OffboardTenant(w.tx, w.repos, w.events, clock=lambda: NOW).execute(TENANT)
    first = w.state.lifecycle[TENANT]
    assert first.purged and first.changed_at == NOW
    later = OffboardTenant(w.tx, w.repos, w.events, clock=lambda: NOW.replace(day=20))
    n = len(w.state.outbox)
    await later.execute(TENANT)
    assert w.state.lifecycle[TENANT] == first and len(w.state.outbox) == n


async def test_phase_two_waits_for_the_soak_then_deletes_rows_and_keeps_the_lifecycle_row() -> None:
    """EVT-15 / TL-4: deletion waits for TENANT_PURGE_SOAK_DAYS; DR_MODE disables it."""
    w = World()
    c = w.add_collection(status=C.PURGING)
    w.state.lifecycle[TENANT] = TenantLifecycle(TENANT, TenantState.PURGED, NOW)

    def purge(now_day: int, *, dr: bool = False) -> PurgeTenantCorpus:
        return PurgeTenantCorpus(
            w.tx, w.repos, soak_days=7, dr_mode=dr, clock=lambda: NOW.replace(day=now_day)
        )

    assert not await purge(5).execute(TENANT) and c.id in w.state.collections
    assert not await purge(20, dr=True).execute(TENANT) and c.id in w.state.collections
    assert await purge(9).execute(TENANT)
    assert not w.state.collections and not w.state.generations
    assert w.state.lifecycle[TENANT].purged  # the record that the purge happened (TL-4)
    assert await purge(10).execute(TENANT) and not w.state.collections  # a repeat is harmless


async def test_phase_two_never_runs_for_a_merely_frozen_tenant() -> None:
    """Only a purged row (and its soak) enables the delete."""
    w = World()
    c = w.add_collection()
    w.state.lifecycle[TENANT] = TenantLifecycle(TENANT, TenantState.FROZEN, NOW.replace(year=2020))
    assert not await PurgeTenantCorpus(w.tx, w.repos, soak_days=7, clock=lambda: NOW).execute(
        TENANT
    )
    assert c.id in w.state.collections


# --- tender_lifecycle -----------------------------------------------------------------------


def _restrict(w: World) -> RestrictTender:
    return RestrictTender(w.tx, w.repos, w.events, clock=lambda: NOW)


async def test_restriction_with_an_existing_row_announces_and_inserts_the_promotion_target() -> (
    None
):
    """COL-7 / COL-8: one transaction; the original keeps serving; the target is provisioning."""
    w = World()
    src = w.add_collection(status=C.ACTIVE)
    assert await _restrict(w).execute(TENANT, TENDER) is RestrictionOutcome.PROMOTION_STARTED
    announced = w.state.collections[src.id]
    assert announced.restricted_announced_at == NOW and announced.status is C.ACTIVE
    [target] = [c for c in w.state.collections.values() if c.restricted]
    assert (target.status, target.promoted_from, target.id) == (
        C.PROVISIONING,
        src.id,
        derive_collection_id(CollectionType.TENDER, TENANT, TENDER, True),
    )
    assert target.migration_id is not None
    assert w.state.generations[(target.id, 1)].status is G.VERIFIED
    [event] = w.state.events("CollectionPromoted")
    assert event.payload["promoted_from"] == str(src.id) and event.payload["restricted"]


async def test_restriction_redelivery_is_idempotent() -> None:
    """LD-33 / EVT-6: a second promotion inserts nothing and publishes nothing."""
    w = World()
    w.add_collection(status=C.ACTIVE)
    await _restrict(w).execute(TENANT, TENDER)
    n_rows, n_events = len(w.state.collections), len(w.state.outbox)
    assert await _restrict(w).execute(TENANT, TENDER) is RestrictionOutcome.ALREADY_RESTRICTED
    assert (len(w.state.collections), len(w.state.outbox)) == (n_rows, n_events)


async def test_restriction_before_any_document_creates_one_restricted_row() -> None:
    """LD-43: no promotion, and a later C-I1 with tender_restricted=false gets this row."""
    w = World()
    assert await _restrict(w).execute(TENANT, TENDER) is RestrictionOutcome.CREATED_RESTRICTED
    [row] = w.state.collections.values()
    assert row.restricted and row.promoted_from is None and row.status is C.PROVISIONING
    [event] = w.state.events("CollectionProvisioned")
    assert event.payload["restricted"] is True
    assert not w.state.events("CollectionPromoted")


async def test_restriction_of_an_unpromotable_source_still_announces() -> None:
    """A frozen source is announced (authorization switches) but not promoted."""
    w = World()
    src = w.add_collection(status=C.FROZEN)
    assert await _restrict(w).execute(TENANT, TENDER) is RestrictionOutcome.ANNOUNCED_ONLY
    assert w.state.collections[src.id].restricted_announced_at == NOW
    assert len(w.state.collections) == 1


async def test_tender_purge_moves_every_row_to_purging_and_deletes_nothing() -> None:
    """COL-13: the row stays; a promotion's two rows both move; a repeat moves none."""
    w = World()
    src = w.add_collection(status=C.ACTIVE)
    tgt = w.add_collection(restricted=True, status=C.PROVISIONING, promoted_from=src.id)
    use = BeginTenderPurge(w.tx, w.repos, w.events)
    assert await use.execute(TENANT, TENDER) == 2
    assert {w.state.collections[i].status for i in (src.id, tgt.id)} == {C.PURGING}
    assert await use.execute(TENANT, TENDER) == 0


async def test_tender_closed_sets_the_timestamp_once_on_every_row() -> None:
    """COL-14: whatever the status, both rows of a promotion; a later outcome closes none."""
    w = World()
    src = w.add_collection(status=C.ACTIVE)
    tgt = w.add_collection(restricted=True, status=C.PROVISIONING, promoted_from=src.id)
    other = w.add_collection(tender_id=uuid4())
    assert (
        await CloseTender(w.tx, w.repos, w.events, clock=lambda: NOW).execute(TENANT, TENDER) == 2
    )
    assert w.state.collections[src.id].tender_closed_at == NOW
    assert w.state.collections[tgt.id].tender_closed_at == NOW
    assert w.state.collections[other.id].tender_closed_at is None
    assert (
        await CloseTender(w.tx, w.repos, w.events, clock=lambda: NOW.replace(day=9)).execute(
            TENANT, TENDER
        )
        == 0
    )
    assert w.state.collections[src.id].tender_closed_at == NOW
