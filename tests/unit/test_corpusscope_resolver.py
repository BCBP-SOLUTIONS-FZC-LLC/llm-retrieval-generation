"""Scope resolution matrix (LLD §6.2, §6.3, §13.2, §4.7.2) over in-memory fakes."""

from __future__ import annotations

import hashlib
import json
from uuid import uuid4

import pytest
from tests.support_scope import (
    DEPT_B,
    OTHER_TENANT,
    TENANT,
    USER,
    Clock,
    DictCache,
    FakeTac4,
    principal,
    resolver,
    row,
)

from rag.corpusscope import (
    Level,
    ScopeLevelInsufficient,
    ScopeNotFound,
    ScopeRequest,
    ScopeStoreUnavailable,
    ScopeToken,
)

NOT_RETRIEVABLE = ["provisioning", "frozen", "purging", "migration_failed", "superseded"]


async def test_ordinary_tender_resolves_from_departments_and_role() -> None:
    """CG-1: departments intersect, then the flat role table (any IAM role -> generate)."""
    c = row("tender")
    r, _ = resolver([c])
    assert (await r.resolve(principal("member"), ScopeRequest())).collections[
        0
    ].level is Level.GENERATE
    assert (await r.resolve(principal(), ScopeRequest())).level is Level.SEARCH


async def test_no_department_intersection_is_404_not_403() -> None:
    """CG-8: a missing grant is indistinguishable from an absent collection."""
    r, _ = resolver([row("tender", departments=(DEPT_B,))])
    with pytest.raises(ScopeNotFound):
        await r.resolve(principal("member"), ScopeRequest())


async def test_tenant_and_reference_collections() -> None:
    """Tenant standing is membership; reference is platform-readable at search."""
    t, ref = row("tenant"), row("reference")
    r, _ = resolver([t, ref])
    token = await r.resolve(principal("tenant_admin"), ScopeRequest())
    levels = {c.type: c.level for c in token.collections}
    assert levels == {"tenant": Level.GENERATE, "reference": Level.SEARCH}


async def test_reference_does_not_lower_the_clamp() -> None:
    """Deviation: reference confers no standing, so it cannot clamp an answer/generate check."""
    r, _ = resolver([row("tender"), row("reference")])
    token = await r.resolve(principal("member"), ScopeRequest(), required=Level.GENERATE)
    assert token.level is Level.GENERATE and not token.clamped


async def test_reference_only_cannot_satisfy_answer() -> None:
    """A scope of reference collections alone holds search and no more."""
    r, _ = resolver([row("reference")])
    with pytest.raises(ScopeLevelInsufficient):
        await r.resolve(principal("member"), ScopeRequest(), required=Level.ANSWER)


async def test_restricted_tender_not_named_never_enters_and_tac4_is_not_called() -> None:
    """ISO-15: whatever grants the caller holds, an unnamed request resolves no restricted row."""
    restricted = row("tender", restricted=True)
    tac4 = FakeTac4({restricted.tender_id: "approve"})
    r, _ = resolver([restricted, row("tenant")], tac4)
    token = await r.resolve(principal("member"), ScopeRequest())
    assert restricted.id not in {c.id for c in token.collections}
    assert tac4.calls == 0


@pytest.mark.parametrize(
    ("answer", "level"),
    [("view", Level.ANSWER), ("edit", Level.GENERATE), ("approve", Level.GENERATE)],
)
async def test_restricted_tender_level_mapping(answer: str, level: Level) -> None:
    """§4.7.2 mapping; a restricted tender never yields search."""
    c = row("tender", restricted=True)
    r, _ = resolver([c], FakeTac4({c.tender_id: answer}))
    token = await r.resolve(principal(), ScopeRequest(tender_id=c.tender_id))
    assert token.collections[0].level is level


async def test_restricted_no_access_is_404_and_unknown_label_fails_closed() -> None:
    """CG-5: a successful no-access answer is a negative; an unknown label confers nothing."""
    c = row("tender", restricted=True)
    for answer in (None, "owner"):
        r, _ = resolver([c], FakeTac4({c.tender_id: answer}))
        with pytest.raises(ScopeNotFound):
            await r.resolve(principal("member"), ScopeRequest(tender_id=c.tender_id))


async def test_announced_restriction_is_restricted_whatever_the_flag_says() -> None:
    """COL-8: restricted_announced_at set means not ordinary, even with restricted = false."""
    c = row("tender", announced=True)
    tac4 = FakeTac4({c.tender_id: "view"})
    r, _ = resolver([c], tac4)
    with pytest.raises(ScopeNotFound):  # departments alone no longer grant it
        await r.resolve(principal("member"), ScopeRequest())
    token = await r.resolve(principal("member"), ScopeRequest(tender_id=c.tender_id))
    assert token.level is Level.ANSWER and tac4.calls == 1


async def test_tac4_unreachable_refuses_after_one_retry_and_spares_other_requests() -> None:
    """ISO-16 / CG-4: 503 for a request naming a restricted tender; none for one that names none."""
    c = row("tender", restricted=True)
    tac4 = FakeTac4({c.tender_id: "edit"})
    tac4.error = True
    r, _ = resolver([c, row("tenant")], tac4)
    with pytest.raises(ScopeStoreUnavailable):
        await r.resolve(principal("member"), ScopeRequest(tender_id=c.tender_id))
    assert tac4.calls == 2  # one retry
    assert (await r.resolve(principal("member"), ScopeRequest())).collections[0].type == "tenant"


@pytest.mark.parametrize("status", NOT_RETRIEVABLE)
async def test_non_active_collection_never_enters_a_token(status: str) -> None:
    """ISO-9 / SCOPE-11: excluded unnamed, and a named one is 404 (indistinguishable)."""
    c = row("tender", status=status)
    keep = row("tenant")
    r, _ = resolver([c, keep])
    token = await r.resolve(principal("member"), ScopeRequest())
    assert [x.id for x in token.collections] == [keep.id]
    with pytest.raises(ScopeNotFound):
        await r.resolve(principal("member"), ScopeRequest(collection_ids=(c.id,)))


async def test_collection_without_active_generation_is_an_empty_corpus() -> None:
    """SCOPE-12: admitted with no generation, reported empty, never an error."""
    c = row("tender", generation=None)
    r, _ = resolver([c])
    token = await r.resolve(principal("member"), ScopeRequest())
    assert token.collections[0].empty and token.collections[0].index_generation is None


async def test_clamp_is_the_lowest_level_and_is_recorded() -> None:
    """SCOPE-8: answer (restricted view) with generate (ordinary) clamps to answer."""
    restricted, ordinary = row("tender", restricted=True), row("tender")
    r, _ = resolver([restricted, ordinary], FakeTac4({restricted.tender_id: "view"}))
    token = await r.resolve(
        principal("member"),
        ScopeRequest(collection_ids=(restricted.id, ordinary.id)),
    )
    assert token.level is Level.ANSWER and token.clamped


async def test_level_below_operation_is_403() -> None:
    """403 is reserved for a held grant at an insufficient level."""
    r, _ = resolver([row("tender")])
    with pytest.raises(ScopeLevelInsufficient):
        await r.resolve(principal(), ScopeRequest(), required=Level.ANSWER)


async def test_named_collection_not_granted_is_404_never_a_silent_narrowing() -> None:
    """§5.5: an explicit collection_ids entry the caller cannot reach fails the request."""
    mine, theirs = row("tenant"), row("tender", departments=(DEPT_B,))
    r, _ = resolver([mine, theirs])
    with pytest.raises(ScopeNotFound):
        await r.resolve(principal("member"), ScopeRequest(collection_ids=(mine.id, theirs.id)))


async def test_adv5_named_tender_without_a_collection_is_404_even_with_tenant_scope() -> None:
    """ADV-5: tender_id routes and filters, never authorizes."""
    r, _ = resolver([row("tenant"), row("reference")])
    with pytest.raises(ScopeNotFound):
        await r.resolve(principal("member"), ScopeRequest(tender_id=uuid4()))


async def test_tender_id_filters_tender_collections_and_keeps_tenant_and_reference() -> None:
    """A named tender selects its collection, and the tenant/reference corpora join it."""
    a, b = row("tender"), row("tender")
    r, _ = resolver([a, b, row("tenant"), row("reference")])
    token = await r.resolve(principal("member"), ScopeRequest(tender_id=a.tender_id))
    ids = {c.id for c in token.collections}
    assert a.id in ids and b.id not in ids and len(ids) == 3


async def test_collection_types_filter() -> None:
    """``collection_types`` narrows the set; nothing left is a 404."""
    r, _ = resolver([row("tender"), row("tenant")])
    token = await r.resolve(principal("member"), ScopeRequest(collection_types=("tenant",)))
    assert {c.type for c in token.collections} == {"tenant"}
    with pytest.raises(ScopeNotFound):
        await r.resolve(principal("member"), ScopeRequest(collection_types=("reference",)))


async def test_foreign_tenant_rows_are_dropped_even_if_the_store_returns_them() -> None:
    """Defence in depth beside RLS: only this tenant's rows and PLATFORM reference rows."""
    foreign = row("tender", tenant=OTHER_TENANT)
    r, _ = resolver([foreign])
    with pytest.raises(ScopeNotFound):
        await r.resolve(principal("member"), ScopeRequest(collection_ids=(foreign.id,)))


async def test_store_failure_retries_once_then_refuses() -> None:
    """§13.2: 100 ms, one retry on another connection, then 503, never a default scope."""
    r, store = resolver([row("tender")])
    store.fail = 1
    assert (await r.resolve(principal("member"), ScopeRequest())).collections
    store.fail = 2
    with pytest.raises(ScopeStoreUnavailable):
        await r.resolve(principal("member"), ScopeRequest())
    assert store.calls == 4


async def test_cache_serves_whole_resolutions_for_60_seconds_only() -> None:
    """SCOPE-4: a hit within the TTL; after it a fresh resolution sees a revoked grant."""
    clock, cache = Clock(), DictCache()
    c = row("tender", restricted=True)
    tac4 = FakeTac4({c.tender_id: "edit"})
    r, store = resolver([c], tac4, cache, clock)
    request = ScopeRequest(tender_id=c.tender_id)
    first = await r.resolve(principal("member"), request)
    clock.advance(30)
    assert await r.resolve(principal("member"), request) == first
    assert store.calls == 1 and tac4.calls == 1
    tac4.answers[c.tender_id] = None  # revoked in IAM; no event reaches us
    clock.advance(31)
    with pytest.raises(ScopeNotFound):
        await r.resolve(principal("member"), request)


async def test_cache_is_keyed_per_principal_standing_and_request() -> None:
    """A different role or department set or request is a different key (never merged)."""
    cache = DictCache()
    r, store = resolver([row("tender"), row("tenant")], cache=cache)
    await r.resolve(principal("member"), ScopeRequest())
    await r.resolve(principal(), ScopeRequest())  # no role: different level
    await r.resolve(principal("member"), ScopeRequest(collection_types=("tenant",)))
    assert store.calls == 3 and len(cache.data) == 3


async def test_cache_failure_and_corruption_are_misses() -> None:
    """§11.4: a broken cache bypasses to source with unchanged correctness."""
    cache = DictCache()
    r, store = resolver([row("tenant")], cache=cache)
    await r.resolve(principal("member"), ScopeRequest())
    key = next(iter(cache.data))
    cache.data[key] = "{not json"
    await r.resolve(principal("member"), ScopeRequest())
    cache.broken = True
    assert (await r.resolve(principal("member"), ScopeRequest())).collections
    assert store.calls == 3


async def test_token_lifetime_and_expiry() -> None:
    """Token TTL 600 s; resolution stamps the generation and purge epoch (SCOPE-5)."""
    clock = Clock()
    c = row("tender", generation=3, epoch=7)
    r, _ = resolver([c], clock=clock)
    token = await r.resolve(principal("member"), ScopeRequest())
    assert (token.expires_at - token.resolved_at).total_seconds() == 600
    assert token.collections[0].index_generation == 3 and token.collections[0].purge_epoch == 7
    token.require_live(clock.now)


async def test_weight_default_per_type_and_override() -> None:
    """HLD §7.3 weights: row override wins, else the per-type default."""
    c, t = row("tender", weight=0.7), row("tenant")
    r, _ = resolver([c, t])
    token = await r.resolve(principal("member"), ScopeRequest())
    weights = {x.type: str(x.weight) for x in token.collections}
    assert weights == {"tender": "0.70", "tenant": "0.90"}


async def test_resolution_hash_is_rs7_canonical_json() -> None:
    """RS-7: sha256 of sorted-key, whitespace-free JSON; order of the rows cannot matter."""
    a, b = row("tender"), row("tenant")
    r1, _ = resolver([a, b])
    r2, _ = resolver([b, a])
    t1 = await r1.resolve(principal("member"), ScopeRequest())
    t2 = await r2.resolve(principal("member"), ScopeRequest())
    expected = {
        "tenant_id": str(TENANT),
        "principal_id": str(USER),
        "collections": sorted(
            ({"id": str(x.id), "level": "generate", "index_generation": 1} for x in (a, b)),
            key=lambda d: d["id"],
        ),
        "tender_ids": [str(a.tender_id)],
    }
    raw = json.dumps(expected, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    assert t1.resolution_hash == t2.resolution_hash == hashlib.sha256(raw.encode()).hexdigest()


async def test_hash_moves_with_level_and_generation() -> None:
    """A replay can prove it ran under the same authorization (RS-7)."""
    c = row("tender")
    base, _ = resolver([c])
    h0 = (await base.resolve(principal("member"), ScopeRequest())).resolution_hash
    h_level = (await base.resolve(principal(), ScopeRequest())).resolution_hash
    bumped, _ = resolver([row_with_generation(c, 2)])
    h_gen = (await bumped.resolve(principal("member"), ScopeRequest())).resolution_hash
    assert len({h0, h_level, h_gen}) == 3


def row_with_generation(c, generation):  # type: ignore[no-untyped-def]
    """The same row at another generation."""
    from dataclasses import replace

    return replace(c, active_generation=generation)


async def test_token_round_trips_through_the_cache_form() -> None:
    """to_json/from_json preserve the token and verify its hash."""
    r, _ = resolver([row("tender"), row("reference")])
    token = await r.resolve(principal("member"), ScopeRequest())
    assert ScopeToken.from_json(token.to_json()) == token
