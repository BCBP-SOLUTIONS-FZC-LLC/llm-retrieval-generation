"""Corpus use cases against in-memory fakes: C-I1, C-I2, C-I4, C-I5, C-I6, C-I8, P-1/2/3/6."""

from __future__ import annotations

from dataclasses import replace
from uuid import uuid4

import pytest
from tests.support_corpus import (
    NOW,
    OTHER_TENANT,
    TENANT,
    TENDER,
    USER,
    FakePoints,
    FakeScope,
    World,
    identity,
)

from rag.application.corpus.commands import (
    ActivateGenerationCommand,
    CollectionQuery,
    ListCollectionsQuery,
    ListGrantsQuery,
    MigrationOutcome,
    MigrationResultCommand,
    PutRetentionCommand,
    ResolveCollectionCommand,
)
from rag.application.corpus.generations import (
    ActivateIndexGeneration,
    BumpPurgeEpoch,
    ReportMigrationResult,
)
from rag.application.corpus.queries import GetCollection, ListCollections, ListGrants
from rag.application.corpus.resolve import GetCollectionPlacement, ResolveOrCreateCollection
from rag.application.corpus.retention import GetRetention, PutRetention
from rag.domain.corpus.collection import PLATFORM, REFERENCE, TENANT_WIDE, derive_collection_id
from rag.domain.corpus.errors import (
    CollectionFrozen,
    CollectionNotFound,
    CollectionNotProvisioned,
    CollectionPurging,
    GenerationNotVerified,
    MigrationIncomplete,
    MigrationMismatch,
    OptimisticLockConflict,
)
from rag.domain.corpus.grant import CollectionGrant
from rag.domain.corpus.tenant_lifecycle import TenantLifecycle, TenantState
from rag.domain.shared.enums import CollectionStatus as C
from rag.domain.shared.enums import (
    CollectionType,
    GrantLevel,
    GrantSubjectType,
)
from rag.domain.shared.enums import IndexGenerationStatus as G


def _resolve(w: World) -> ResolveOrCreateCollection:
    return ResolveOrCreateCollection(w.tx, w.repos, w.events, clock=lambda: NOW)


def _cmd(restricted: bool | None = None, tender=TENDER, tenant=TENANT) -> ResolveCollectionCommand:  # type: ignore[no-untyped-def]
    return ResolveCollectionCommand(tenant, tender, restricted)


# --- C-I1 -----------------------------------------------------------------------------------


async def test_first_document_creates_provisioning_collection_with_verified_gen1() -> None:
    """201 body: derived id, provisioning, generation 1 verified, event enqueued (§5.7.17)."""
    w = World()
    result = await _resolve(w).execute(_cmd())
    p = result.placement
    assert result.created
    assert p.collection_id == derive_collection_id(CollectionType.TENDER, TENANT, TENDER, False)
    assert (p.status, p.active_index_generation, p.restricted, p.knowledge_level) == (
        "provisioning",
        1,
        False,
        "TENDER",
    )
    gen = w.state.generations[(p.collection_id, 1)]
    assert (gen.status, gen.verified_at) == (G.VERIFIED, NOW)
    [event] = w.state.events("CollectionProvisioned")
    assert event.payload["status"] == "provisioning" and event.tenant_id == TENANT


async def test_second_call_returns_200_same_body() -> None:
    """A repeat finds the row: created is false and the body is unchanged."""
    w = World()
    first = await _resolve(w).execute(_cmd())
    second = await _resolve(w).execute(_cmd())
    assert not second.created and second.placement == first.placement
    assert len(w.state.events("CollectionProvisioned")) == 1


async def test_tender_restricted_true_creates_a_restricted_row_without_promotion() -> None:
    """§5.7.16: advisory flag, creation only; own placement; no promoted_from."""
    w = World()
    p = (await _resolve(w).execute(_cmd(restricted=True))).placement
    assert p.restricted
    assert w.state.collections[p.collection_id].promoted_from is None
    assert "__tender__" in p.physical_collection


async def test_absent_flag_behaves_as_false() -> None:
    """LD-97."""
    w = World()
    assert not (await _resolve(w).execute(_cmd(restricted=None))).placement.restricted


async def test_lookup_ignores_restricted_so_a_stale_false_gets_the_restricted_row() -> None:
    """LD-43: a restricted row exists; a stale tender_restricted=false must not create a sibling."""
    w = World()
    w.add_collection(restricted=True, status=C.ACTIVE)
    result = await _resolve(w).execute(_cmd(restricted=False))
    assert not result.created and result.placement.restricted
    assert len(w.state.collections) == 1


async def test_active_original_is_preferred_over_the_provisioning_target() -> None:
    """COL-9: during a promotion Ingestion keeps writing to the serving row."""
    w = World()
    src = w.add_collection(status=C.ACTIVE)
    w.add_collection(restricted=True, status=C.PROVISIONING, promoted_from=src.id)
    assert (await _resolve(w).execute(_cmd())).placement.collection_id == src.id


@pytest.mark.parametrize(
    ("state", "exc"),
    [(TenantState.PURGED, CollectionPurging), (TenantState.FROZEN, CollectionFrozen)],
)
async def test_tenant_lifecycle_refuses_before_any_lookup(state: TenantState, exc: type) -> None:
    """TL-3: purged is 410, frozen is 409, and nothing is created for a tenant with no rows."""
    w = World()
    w.state.lifecycle[TENANT] = TenantLifecycle(TENANT, state, NOW)
    with pytest.raises(exc):
        await _resolve(w).execute(_cmd())
    assert not w.state.collections


async def test_other_tenants_lifecycle_does_not_refuse() -> None:
    """The record is the request tenant's own."""
    w = World()
    w.state.lifecycle[OTHER_TENANT] = TenantLifecycle(OTHER_TENANT, TenantState.PURGED, NOW)
    assert (await _resolve(w).execute(_cmd())).created


@pytest.mark.parametrize(
    ("status", "exc"), [(C.FROZEN, CollectionFrozen), (C.PURGING, CollectionPurging)]
)
async def test_frozen_and_purging_rows_are_refused(status: C, exc: type) -> None:
    """A collection row answers 409 / 410 (COL-13: a purged tender stays refused)."""
    w = World()
    w.add_collection(status=status)
    with pytest.raises(exc):
        await _resolve(w).execute(_cmd())


async def test_reference_corpus_is_never_created_by_an_upload() -> None:
    """COL-3 / COL-5: 409 COLLECTION_NOT_PROVISIONED."""
    w = World()
    with pytest.raises(CollectionNotProvisioned):
        await _resolve(w).execute(_cmd(tender=REFERENCE))
    assert not w.state.collections


async def test_existing_reference_collection_is_returned() -> None:
    """A provisioned reference corpus resolves for any tenant."""
    w = World()
    ref = w.add_collection(tender_id=REFERENCE, tenant_id=PLATFORM)
    result = await _resolve(w).execute(_cmd(tender=REFERENCE))
    assert result.placement.collection_id == ref.id and not result.created


async def test_tenant_wide_collection_is_created_active_with_active_gen1() -> None:
    """§5.7.17: no ingestion campaign to trigger an activation."""
    w = World()
    p = (await _resolve(w).execute(_cmd(tender=TENANT_WIDE))).placement
    assert (p.type, p.status, p.knowledge_level) == ("tenant", "active", "TENANT_WIDE")
    assert w.state.generations[(p.collection_id, 1)].status is G.ACTIVE


async def test_race_on_the_derived_primary_key_resolves_to_one_row() -> None:
    """COL-11: the loser's insert is a no-op and it re-reads the winner (never two rows)."""
    w = World()
    use = _resolve(w)
    real_find = w.repos.collections.find_by_tender
    calls = 0

    async def racing_find(tx, *a, **kw):  # type: ignore[no-untyped-def]
        nonlocal calls
        calls += 1
        if calls == 1:  # the first lookup sees nothing; a concurrent call then commits the row
            await _resolve(w).execute(_cmd())
            return []
        return await real_find(tx, *a, **kw)

    w.repos.collections.find_by_tender = racing_find  # type: ignore[method-assign]
    result = await use.execute(_cmd())
    assert not result.created
    assert len(w.state.collections) == 1
    assert len(w.state.events("CollectionProvisioned")) == 1


async def test_rollback_drops_the_event_with_the_row() -> None:
    """EVT-8: a failure after the insert leaves neither the row nor its event."""
    w = World()

    async def boom(tx, g):  # type: ignore[no-untyped-def]
        raise RuntimeError("boom")

    w.repos.generations.insert = boom  # type: ignore[method-assign]
    with pytest.raises(RuntimeError):
        await _resolve(w).execute(_cmd())
    assert not w.state.collections and not w.state.outbox


# --- C-I2 -----------------------------------------------------------------------------------


@pytest.mark.parametrize("status", list(C))
async def test_placement_answers_in_every_status(status: C) -> None:
    """C-I2: purging included (COL-13)."""
    w = World()
    c = w.add_collection(status=status)
    p = await GetCollectionPlacement(w.tx, w.repos).execute(c.id)
    assert (p.status, p.physical_collection) == (status.value, c.physical_collection)


async def test_placement_unknown_is_404() -> None:
    """COLLECTION_NOT_FOUND."""
    with pytest.raises(CollectionNotFound):
        await GetCollectionPlacement(World().tx, World().repos).execute(uuid4())


# --- C-I4 -----------------------------------------------------------------------------------


def _activate(w: World) -> ActivateIndexGeneration:
    return ActivateIndexGeneration(w.tx, w.repos, w.events, clock=lambda: NOW)


async def test_first_activation_flips_provisioning_to_active_and_previous_is_null() -> None:
    """IG-4 / LD-94, and the event is enqueued in the same transaction."""
    w = World()
    c = w.add_collection(status=C.PROVISIONING)
    out = await _activate(w).execute(ActivateGenerationCommand(TENANT, c.id, 1, c.record_version))
    assert (out.active_index_generation, out.previous, out.record_version) == (1, None, 2)
    assert w.state.collections[c.id].status is C.ACTIVE
    assert w.state.generations[(c.id, 1)].status is G.ACTIVE
    [event] = w.state.events("IndexGenerationActivated")
    assert event.payload["generation"] == 1 and event.payload["previous_generation"] is None


async def test_cutover_retires_the_previous_generation_in_the_same_transaction() -> None:
    """IG-1 / IG-3: uq_gen_active admits one active generation."""
    w = World()
    c = w.add_collection(status=C.ACTIVE)
    w.add_generation(c, 2, G.VERIFIED)
    out = await _activate(w).execute(ActivateGenerationCommand(TENANT, c.id, 2, c.record_version))
    assert (out.active_index_generation, out.previous) == (2, 1)
    assert w.state.generations[(c.id, 1)].status is G.RETIRED
    assert w.state.generations[(c.id, 2)].status is G.ACTIVE
    assert w.state.collections[c.id].active_index_generation == 2


async def test_repeat_activation_is_200_whatever_the_version_and_changes_nothing() -> None:
    """LD-37: a retry after a lost response; previous is what the first call returned."""
    w = World()
    c = w.add_collection(status=C.ACTIVE)
    w.add_generation(c, 2, G.VERIFIED)
    first = await _activate(w).execute(ActivateGenerationCommand(TENANT, c.id, 2, c.record_version))
    events = len(w.state.outbox)
    again = await _activate(w).execute(ActivateGenerationCommand(TENANT, c.id, 2, 999))
    assert again == first
    assert len(w.state.outbox) == events


@pytest.mark.parametrize("status", [G.REGISTERED, G.BUILDING, G.FAILED, G.RETIRED])
async def test_only_a_verified_generation_activates(status: G) -> None:
    """IG-5: 409 GENERATION_NOT_VERIFIED, before the version check."""
    w = World()
    c = w.add_collection(status=C.ACTIVE)
    w.add_generation(c, 2, status)
    with pytest.raises(GenerationNotVerified):
        await _activate(w).execute(ActivateGenerationCommand(TENANT, c.id, 2, 12345))
    assert not w.state.events("IndexGenerationActivated")


async def test_stale_record_version_conflicts_and_carries_the_current_one() -> None:
    """OPTIMISTIC_LOCK_CONFLICT with recordVersion; nothing changed."""
    w = World()
    c = w.add_collection(status=C.ACTIVE)
    w.add_generation(c, 2, G.VERIFIED)
    with pytest.raises(OptimisticLockConflict) as err:
        await _activate(w).execute(ActivateGenerationCommand(TENANT, c.id, 2, c.record_version + 5))
    assert err.value.extensions == {"recordVersion": c.record_version}
    assert w.state.generations[(c.id, 2)].status is G.VERIFIED


async def test_activation_404s_and_frozen_refusal() -> None:
    """Unknown collection or generation is 404; a frozen collection is not activated."""
    w = World()
    c = w.add_collection(status=C.ACTIVE)
    with pytest.raises(CollectionNotFound):
        await _activate(w).execute(ActivateGenerationCommand(TENANT, uuid4(), 1, 1))
    with pytest.raises(CollectionNotFound):
        await _activate(w).execute(ActivateGenerationCommand(TENANT, c.id, 9, 1))
    frozen = w.add_collection(status=C.FROZEN, tender_id=uuid4())
    w.add_generation(frozen, 2, G.VERIFIED)
    with pytest.raises(CollectionFrozen):
        await _activate(w).execute(ActivateGenerationCommand(TENANT, frozen.id, 2, 1))


# --- C-I5, C-I6 -----------------------------------------------------------------------------


async def test_purge_epoch_increments_atomically_and_404s() -> None:
    """C-I6: returns the new epoch; no record_version taken."""
    w = World()
    c = w.add_collection()
    use = BumpPurgeEpoch(w.tx, w.repos)
    assert (await use.execute(c.id)).purge_epoch == 1
    assert (await use.execute(c.id)).purge_epoch == 2
    with pytest.raises(CollectionNotFound):
        await use.execute(uuid4())


async def test_get_retention() -> None:
    """C-I5: five fields once P-6 created the policy; 404 before, and for an unknown id."""
    w = World()
    c = w.add_collection()
    use = GetRetention(w.tx, w.repos)
    with pytest.raises(CollectionNotFound):
        await use.execute(c.id)
    await PutRetention(w.tx, w.repos, FakeScope(c.id)).execute(
        PutRetentionCommand(identity("tenant_admin"), c.id, 30, True)
    )
    dto = await use.execute(c.id)
    assert (dto.retain_days, dto.legal_hold, dto.record_version) == (30, True, 1)
    with pytest.raises(CollectionNotFound):
        await use.execute(uuid4())


# --- C-I8 -----------------------------------------------------------------------------------


def _promotion(w: World):  # type: ignore[no-untyped-def]
    src = w.add_collection(status=C.ACTIVE)
    mid = uuid4()
    tgt = w.add_collection(
        restricted=True, status=C.PROVISIONING, promoted_from=src.id, migration_id=mid
    )
    return src, tgt, mid


def _report(w: World, points: int = 100) -> ReportMigrationResult:
    return ReportMigrationResult(w.tx, w.repos, w.events, FakePoints(points), clock=lambda: NOW)


async def test_complete_cuts_over_atomically() -> None:
    """§13.5: source superseded then target active, in one transaction; exactly one active row."""
    w = World()
    src, tgt, mid = _promotion(w)
    out = await _report(w).execute(
        MigrationResultCommand(TENANT, tgt.id, mid, MigrationOutcome.COMPLETE, points_copied=100)
    )
    assert out.status == "active" and out.record_version == 2
    assert w.state.collections[src.id].status is C.SUPERSEDED
    assert w.state.collections[tgt.id].status is C.ACTIVE
    assert w.state.generations[(tgt.id, 1)].status is G.ACTIVE
    reasons = {e.payload["reason"] for e in w.state.events("CollectionStatusChanged")}
    assert reasons == {"promotion_cutover"} and len(w.state.events("CollectionStatusChanged")) == 2


async def test_repeat_complete_is_idempotent() -> None:
    """A repeat of a processed result returns 200 with the same body, changing nothing."""
    w = World()
    _, tgt, mid = _promotion(w)
    cmd = MigrationResultCommand(TENANT, tgt.id, mid, MigrationOutcome.COMPLETE, points_copied=100)
    first = await _report(w).execute(cmd)
    n = len(w.state.outbox)
    assert await _report(w).execute(cmd) == first
    assert len(w.state.outbox) == n


async def test_failed_moves_the_target_to_migration_failed_and_source_keeps_serving() -> None:
    """C-I8 failed: target migration_failed, source stays active; repeat is idempotent."""
    w = World()
    src, tgt, mid = _promotion(w)
    cmd = MigrationResultCommand(
        TENANT, tgt.id, mid, MigrationOutcome.FAILED, reason="qdrant_write_timeout"
    )
    out = await _report(w).execute(cmd)
    assert out.status == "migration_failed"
    assert w.state.collections[src.id].status is C.ACTIVE
    assert (await _report(w).execute(cmd)) == out
    [event] = w.state.events("CollectionStatusChanged")
    assert event.payload["reason"] == "migration_failed"


async def test_points_below_the_source_count_refuse_the_cutover() -> None:
    """MIGRATION_INCOMPLETE carries pointsExpected / pointsReported; nothing changes."""
    w = World()
    src, tgt, mid = _promotion(w)
    with pytest.raises(MigrationIncomplete) as err:
        await _report(w, points=100).execute(
            MigrationResultCommand(TENANT, tgt.id, mid, MigrationOutcome.COMPLETE, points_copied=99)
        )
    assert err.value.extensions == {"pointsExpected": 100, "pointsReported": 99}
    assert w.state.collections[tgt.id].status is C.PROVISIONING
    assert w.state.collections[src.id].status is C.ACTIVE


async def test_wrong_migration_id_or_non_target_is_a_mismatch() -> None:
    """MIGRATION_MISMATCH: not the recorded attempt, or not a promotion target."""
    w = World()
    src, tgt, mid = _promotion(w)
    use = _report(w)
    with pytest.raises(MigrationMismatch):
        await use.execute(
            MigrationResultCommand(TENANT, tgt.id, uuid4(), MigrationOutcome.FAILED, reason="x")
        )
    with pytest.raises(MigrationMismatch):
        await use.execute(
            MigrationResultCommand(TENANT, src.id, mid, MigrationOutcome.FAILED, reason="x")
        )
    with pytest.raises(CollectionNotFound):
        await use.execute(
            MigrationResultCommand(TENANT, uuid4(), mid, MigrationOutcome.FAILED, reason="x")
        )


async def test_result_after_the_opposite_outcome_is_a_mismatch() -> None:
    """A target already migration_failed cannot then be cut over."""
    w = World()
    _, tgt, mid = _promotion(w)
    await _report(w).execute(
        MigrationResultCommand(TENANT, tgt.id, mid, MigrationOutcome.FAILED, reason="x")
    )
    with pytest.raises(MigrationMismatch):
        await _report(w).execute(
            MigrationResultCommand(
                TENANT, tgt.id, mid, MigrationOutcome.COMPLETE, points_copied=100
            )
        )


# --- P-1, P-2, P-3, P-6 ---------------------------------------------------------------------


async def test_list_collections_is_scope_resolved_and_empty_scope_is_empty_list() -> None:
    """P-1: only resolved ids are listed; an empty scope is 200 []."""
    w = World()
    a = w.add_collection()
    w.add_collection(tender_id=uuid4())
    page = await ListCollections(w.tx, w.repos, FakeScope(a.id)).execute(
        ListCollectionsQuery(identity(), 100, 0)
    )
    assert [c.id for c in page.collections] == [a.id]
    assert not hasattr(page.collections[0], "physical_collection")
    empty = await ListCollections(w.tx, w.repos, FakeScope()).execute(
        ListCollectionsQuery(identity(), 100, 0)
    )
    assert empty.collections == []


async def test_get_collection_out_of_scope_is_404_never_403() -> None:
    """P-2 / CG-8."""
    w = World()
    a = w.add_collection()
    with pytest.raises(CollectionNotFound):
        await GetCollection(w.tx, w.repos, FakeScope()).execute(CollectionQuery(identity(), a.id))
    dto = await GetCollection(w.tx, w.repos, FakeScope(a.id)).execute(
        CollectionQuery(identity(), a.id)
    )
    assert dto.id == a.id and dto.record_version == 1


async def test_list_grants_reports_freshness_and_is_never_authoritative() -> None:
    """P-3."""
    w = World()
    c = w.add_collection(restricted=True, status=C.ACTIVE)
    w.state.grants.append(
        CollectionGrant(uuid4(), c.id, TENANT, GrantSubjectType.USER, USER, GrantLevel.ANSWER, NOW)
    )
    page = await ListGrants(w.tx, w.repos, FakeScope(c.id), bound_seconds=60).execute(
        ListGrantsQuery(identity("tenant_admin"), c.id, 100, 0)
    )
    assert page.authoritative is False and page.as_of == NOW and page.bound_seconds == 60
    assert [(e.subject_id, e.level) for e in page.entries] == [(USER, "answer")]
    with pytest.raises(CollectionNotFound):
        await ListGrants(w.tx, w.repos, FakeScope(), bound_seconds=60).execute(
            ListGrantsQuery(identity("tenant_admin"), c.id, 100, 0)
        )


async def test_put_retention_first_write_creates_then_versioned_updates() -> None:
    """P-6 / LD-76: no version creates (201); a version updates (200); everything else 409."""
    w = World()
    c = w.add_collection()
    use = PutRetention(w.tx, w.repos, FakeScope(c.id))
    admin = identity("tenant_admin")

    created = await use.execute(PutRetentionCommand(admin, c.id, 30, False))
    assert created.created and created.policy.record_version == 1

    with pytest.raises(OptimisticLockConflict) as no_version:  # a policy exists, no version sent
        await use.execute(PutRetentionCommand(admin, c.id, 60, False))
    assert no_version.value.extensions == {"recordVersion": 1}

    updated = await use.execute(PutRetentionCommand(admin, c.id, 60, True, record_version=1))
    assert not updated.created and (updated.policy.retain_days, updated.policy.record_version) == (
        60,
        2,
    )

    with pytest.raises(OptimisticLockConflict) as stale:
        await use.execute(PutRetentionCommand(admin, c.id, 90, True, record_version=1))
    assert stale.value.extensions == {"recordVersion": 2}


async def test_put_retention_versioned_write_without_a_policy_carries_no_version() -> None:
    """409 where no policy exists: the caller retries without one."""
    w = World()
    c = w.add_collection()
    with pytest.raises(OptimisticLockConflict) as err:
        await PutRetention(w.tx, w.repos, FakeScope(c.id)).execute(
            PutRetentionCommand(identity("tenant_admin"), c.id, 30, False, record_version=3)
        )
    assert err.value.extensions == {}


async def test_put_retention_out_of_scope_is_404() -> None:
    """The collection is found through scope resolution."""
    w = World()
    c = w.add_collection()
    with pytest.raises(CollectionNotFound):
        await PutRetention(w.tx, w.repos, FakeScope()).execute(
            PutRetentionCommand(identity("tenant_admin"), c.id, 30, False)
        )
    assert replace(c).id == c.id
