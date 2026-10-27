"""Corpus domain rules: COL-10/11/12, IG-3/5/6, TL-1/2, RP-1 and the SQL they mirror."""

from __future__ import annotations

import re
from dataclasses import replace
from pathlib import Path
from typing import Any
from uuid import UUID, uuid5

import pytest
from tests.support_corpus import NOW, TENANT, TENDER, USER

from rag.domain.corpus.collection import (
    COLLECTION_NAMESPACE,
    PLATFORM,
    REFERENCE,
    TENANT_WIDE,
    Collection,
    collection_type_for,
    derive_collection_id,
    derive_placement,
    pick_resolvable,
)
from rag.domain.corpus.errors import (
    GenerationNotVerified,
    InvalidTransition,
    OptimisticLockConflict,
)
from rag.domain.corpus.index_generation import _EDGES, IndexGeneration, is_allowed_edge
from rag.domain.corpus.retention import RetentionPolicy
from rag.domain.corpus.tenant_lifecycle import (
    TenantAction,
    TenantLifecycle,
    TenantState,
    action_for_status,
)
from rag.domain.shared.enums import CollectionStatus as C
from rag.domain.shared.enums import CollectionType
from rag.domain.shared.enums import IndexGenerationStatus as G
from rag.infrastructure.config import COLLECTION_NAMESPACE_DEFAULT

MIGRATION = Path(__file__).resolve().parents[2] / "migrations/corpus/0002_corpus_tables.up.sql"


def _row(status: C = C.ACTIVE, **kw: Any) -> Collection:
    c = Collection.create(tenant_id=TENANT, tender_id=TENDER, restricted=False)
    return replace(c, status=status, **kw)


# --- COL-11 ---------------------------------------------------------------------------------


def test_namespace_constant_matches_config_default() -> None:
    """The domain constant and the §17 default are the one value."""
    assert str(COLLECTION_NAMESPACE) == COLLECTION_NAMESPACE_DEFAULT


def test_id_recipe_is_the_lld_string() -> None:
    """``UUIDv5(ns, "{type}:{tenant}:{tender}:{restricted}")``, lower case, true/false."""
    ns = UUID("8d9d8e69-7546-4011-bf89-15ef4c822386")
    t, d = (
        UUID("00000000-0000-0000-0000-00000000000A"),
        UUID("00000000-0000-0000-0000-00000000000B"),
    )
    expected = uuid5(
        ns,
        "tender:00000000-0000-0000-0000-00000000000a:00000000-0000-0000-0000-00000000000b:false",
    )
    assert derive_collection_id(CollectionType.TENDER, t, d, False) == expected
    assert derive_collection_id(CollectionType.TENDER, t, d, True) != expected


def test_id_depends_on_all_four_inputs() -> None:
    """Type, tenant, tender and restricted each change the id; the same inputs never do."""
    ids = {
        derive_collection_id(CollectionType.TENDER, TENANT, TENDER, False),
        derive_collection_id(CollectionType.TENDER, TENANT, TENDER, True),
        derive_collection_id(CollectionType.TENANT, TENANT, TENDER, False),
        derive_collection_id(CollectionType.TENDER, TENANT, TENANT, False),
        derive_collection_id(CollectionType.TENDER, PLATFORM, TENDER, False),
    }
    assert len(ids) == 5
    assert derive_collection_id(CollectionType.TENDER, TENANT, TENDER, False) in ids


def test_known_vector_is_stable() -> None:
    """A fixed input has a fixed id (guards the string recipe against refactors)."""
    got = derive_collection_id(CollectionType.TENDER, UUID(int=1), UUID(int=2), False)
    assert got == uuid5(
        COLLECTION_NAMESPACE,
        "tender:00000000-0000-0000-0000-000000000001:00000000-0000-0000-0000-000000000002:false",
    )
    # pinned: any change to the recipe or namespace renames every collection row (and Qdrant point)
    assert str(got) == "78f8023b-ae44-5287-bc9a-d7739fab908d"
    assert str(derive_collection_id(CollectionType.TENDER, UUID(int=1), UUID(int=2), True)) == (
        "771bba8b-fc3b-5512-b19d-70b40895df8a"
    )


# --- creation -------------------------------------------------------------------------------


def test_create_tender_is_provisioning_and_tenant_reference_are_active() -> None:
    """§5.7.17: a tender row waits for C-I4; tenant and reference rows start active."""
    assert (
        Collection.create(tenant_id=TENANT, tender_id=TENDER, restricted=False).status
        is C.PROVISIONING
    )
    tenant = Collection.create(tenant_id=TENANT, tender_id=TENANT_WIDE, restricted=False)
    ref = Collection.create(tenant_id=TENANT, tender_id=REFERENCE, restricted=False)
    assert (tenant.type, tenant.status) == (CollectionType.TENANT, C.ACTIVE)
    assert (ref.type, ref.status, ref.tenant_id) == (CollectionType.REFERENCE, C.ACTIVE, PLATFORM)


def test_type_follows_the_tender_sentinels() -> None:
    """The sentinels name the non-tender types (§4.10)."""
    assert collection_type_for(TENANT_WIDE) is CollectionType.TENANT
    assert collection_type_for(REFERENCE) is CollectionType.REFERENCE
    assert collection_type_for(TENDER) is CollectionType.TENDER


def test_placement_derives_from_restricted_only() -> None:
    """COL-1: restricted gets a physical collection of its own; ordinary shares the tenant's."""
    shared = derive_placement(CollectionType.TENDER, TENANT, TENDER, False)
    own = derive_placement(CollectionType.TENDER, TENANT, TENDER, True)
    other = derive_placement(CollectionType.TENDER, TENANT, UUID(int=7), False)
    assert shared[0] == other[0] != own[0]
    assert shared[1] != other[1]


# --- status machine -------------------------------------------------------------------------


def test_retrievable_active_only_writable_three() -> None:
    """COL-10."""
    for status in C:
        row = _row(status)
        assert row.retrievable == (status is C.ACTIVE)
        assert row.writable == (status in {C.PROVISIONING, C.ACTIVE, C.MIGRATION_FAILED})


@pytest.mark.parametrize("before", [C.PROVISIONING, C.ACTIVE, C.MIGRATION_FAILED])
def test_freeze_then_restore_returns_the_interrupted_status(before: C) -> None:
    """COL-12: restore is to the recorded status, never blindly to active."""
    frozen = _row(before).freeze()
    assert (frozen.status, frozen.status_before_freeze) == (C.FROZEN, before)
    restored = frozen.restore()
    assert (restored.status, restored.status_before_freeze) == (before, None)


@pytest.mark.parametrize("status", [C.FROZEN, C.PURGING, C.SUPERSEDED])
def test_freeze_refused_outside_freezable_states(status: C) -> None:
    """Only provisioning, active and migration_failed freeze."""
    with pytest.raises(InvalidTransition):
        _row(status).freeze()


def test_restore_requires_frozen() -> None:
    """A row that is not frozen cannot be restored."""
    with pytest.raises(InvalidTransition):
        _row(C.ACTIVE).restore()


def test_purge_from_frozen_nulls_status_before_freeze() -> None:
    """ck_coll_freeze: the column is present exactly while frozen."""
    purging = _row(C.ACTIVE).freeze().begin_purge()
    assert (purging.status, purging.status_before_freeze) == (C.PURGING, None)


def test_terminal_states_have_no_exit() -> None:
    """purging and superseded are terminal."""
    for status in (C.PURGING, C.SUPERSEDED):
        row = _row(status)
        for op in ("freeze", "restore", "begin_purge", "supersede", "activate", "fail_migration"):
            with pytest.raises(InvalidTransition):
                getattr(row, op)()


def test_promotion_edges() -> None:
    """The promotion edges: provisioning to active or migration_failed, and back on retry."""
    assert _row(C.PROVISIONING).activate().status is C.ACTIVE
    failed = _row(C.PROVISIONING).fail_migration()
    assert failed.status is C.MIGRATION_FAILED and failed.retry_migration().status is C.PROVISIONING
    assert _row(C.ACTIVE).supersede().status is C.SUPERSEDED
    with pytest.raises(InvalidTransition):
        _row(C.PROVISIONING).supersede()


def test_announce_and_close_are_set_once() -> None:
    """COL-8 and COL-14: the first timestamp wins."""
    first = _row().announce_restricted(NOW)
    assert first.announce_restricted(NOW.replace(year=2030)).restricted_announced_at == NOW
    closed = _row().close_tender(NOW)
    assert closed.close_tender(NOW.replace(year=2030)).tender_closed_at == NOW


def test_pick_resolvable_prefers_active_and_ignores_restricted() -> None:
    """§5.7.16: active over provisioning; the lookup never keys on restricted."""
    prov = _row(C.PROVISIONING, restricted=True, promoted_from=UUID(int=1))
    act = _row(C.ACTIVE)
    assert pick_resolvable([prov, act]) is act
    assert pick_resolvable([prov]) is prov
    assert pick_resolvable([]) is None
    assert pick_resolvable([_row(C.MIGRATION_FAILED)]) is None  # never the answer
    assert pick_resolvable([_row(C.FROZEN)]).status is C.FROZEN  # type: ignore[union-attr]


# --- index generations ----------------------------------------------------------------------


def _gen(status: G) -> IndexGeneration:
    return IndexGeneration(UUID(int=1), 2, TENANT, "e", "c", status, NOW, verified_at=NOW)


def test_first_generation_is_verified_for_tender_active_otherwise() -> None:
    """IG-3 / §5.7.17."""
    v = IndexGeneration.first(collection_id=UUID(int=1), tenant_id=TENANT, active=False, now=NOW)
    a = IndexGeneration.first(collection_id=UUID(int=1), tenant_id=TENANT, active=True, now=NOW)
    assert (v.generation, v.status, v.verified_at, v.activated_at) == (1, G.VERIFIED, NOW, None)
    assert (a.status, a.activated_at) == (G.ACTIVE, NOW)


def test_activate_only_from_verified() -> None:
    """IG-5: any other status is GENERATION_NOT_VERIFIED."""
    assert _gen(G.VERIFIED).activate(NOW).status is G.ACTIVE
    for status in set(G) - {G.VERIFIED}:
        with pytest.raises(GenerationNotVerified):
            _gen(status).activate(NOW)


def test_failed_and_retired_are_terminal() -> None:
    """IG-6."""
    for status in (G.FAILED, G.RETIRED):
        assert not any(is_allowed_edge(status, to) for to in G)


def test_edge_matrix_equals_the_trigger_function() -> None:
    """The Python matrix and ``reject_generation_transition`` list the same five edges."""
    sql = MIGRATION.read_text()
    body = sql[sql.index("CREATE FUNCTION reject_generation_transition") :]
    body = body[: body.index("END $$;")]
    sql_edges = {
        (G(a), G(b))
        for a, b in re.findall(
            r"OLD\.status = '(\w+)'\s+AND NEW\.status = '(\w+)'\s+AND by_us", body
        )
    }
    assert sql_edges == set(_EDGES)


def test_machine_methods_walk_the_happy_path() -> None:
    """registered -> building -> verified -> active -> retired, stamping each timestamp."""
    g = IndexGeneration(UUID(int=1), 3, TENANT, "e", "c", G.REGISTERED)
    g = g.begin_build().verify(NOW).activate(NOW).retire(NOW)
    assert (g.status, g.verified_at, g.activated_at, g.retired_at) == (G.RETIRED, NOW, NOW, NOW)
    failed = IndexGeneration(UUID(int=1), 4, TENANT, "e", "c", G.BUILDING).fail(["x"])
    assert (failed.status, failed.failed_checks) == (G.FAILED, ["x"])
    with pytest.raises(InvalidTransition):
        IndexGeneration(UUID(int=1), 5, TENANT, "e", "c", G.REGISTERED).verify(NOW)


# --- tenant lifecycle and retention ---------------------------------------------------------


@pytest.mark.parametrize(
    ("status", "action"),
    [
        ("suspended", TenantAction.FREEZE),
        ("trial_expired", TenantAction.FREEZE),
        ("trial", TenantAction.RESTORE),
        ("active", TenantAction.RESTORE),
        ("past_due", TenantAction.IGNORE),
        ("cancelled", TenantAction.IGNORE),
        ("offboarded", TenantAction.IGNORE),
        ("something_new", TenantAction.IGNORE),
    ],
)
def test_iam_status_mapping(status: str, action: TenantAction) -> None:
    """LD-46's table; offboarded is the purge consumer's alone (EVT-15)."""
    assert action_for_status(status) is action


def test_purged_is_terminal_and_keeps_its_clock() -> None:
    """TL-1/TL-2: frozen -> purged is allowed, the reverse is not, a repeat keeps changed_at."""
    frozen = TenantLifecycle(TENANT, TenantState.FROZEN, NOW)
    purged = frozen.purge(NOW.replace(day=3))
    assert purged.purged and purged.purge(NOW.replace(day=20)).changed_at == NOW.replace(day=3)
    with pytest.raises(InvalidTransition):
        purged.refreeze("x")


def test_soak_counts_from_changed_at() -> None:
    """EVT-15: phase two is due TENANT_PURGE_SOAK_DAYS after changed_at."""
    purged = TenantLifecycle(TENANT, TenantState.PURGED, NOW)
    assert not purged.soak_elapsed(NOW.replace(day=8), 7)
    assert purged.soak_elapsed(NOW.replace(day=9), 7)
    assert not TenantLifecycle(TENANT, TenantState.FROZEN, NOW).soak_elapsed(
        NOW.replace(year=2030), 7
    )


def test_retention_validates_and_version_checks() -> None:
    """retain_days > 0; a stale record_version is a conflict that carries the current one."""
    with pytest.raises(ValueError):
        RetentionPolicy(UUID(int=1), TENANT, 0, False, USER)
    p = RetentionPolicy(UUID(int=1), TENANT, 30, False, USER, record_version=4)
    assert (
        p.updated(retain_days=60, legal_hold=True, updated_by=USER, expected_version=4).retain_days
        == 60
    )
    with pytest.raises(OptimisticLockConflict) as err:
        p.updated(retain_days=60, legal_hold=True, updated_by=USER, expected_version=3)
    assert err.value.extensions == {"recordVersion": 4}
    assert p.swept and not RetentionPolicy(UUID(int=1), TENANT, 30, True, USER).swept  # RP-1
