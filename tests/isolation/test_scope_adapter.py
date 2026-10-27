"""ISO / ADV cases of LLD §19.5-19.6 that run without infrastructure, through the adapters."""

from __future__ import annotations

from uuid import uuid4

import pytest
from tests.support_scope import (
    DEPT_A,
    OTHER_TENANT,
    TENANT,
    USER,
    Clock,
    DictCache,
    FakeTac4,
    resolver,
    row,
)

from rag.application.retrieval.scope import ScopeSpec
from rag.application.shared.scope import ScopeTarget
from rag.corpusscope import ScopeRequest
from rag.domain.shared.enums import GrantLevel
from rag.domain.shared.errors import DomainError, ErrorCode
from rag.domain.shared.identity import DepartmentRole, RequestIdentity
from rag.infrastructure.scope.adapter import CorpusScopeAdapter


def identity(*roles: str, tenant=TENANT) -> RequestIdentity:  # type: ignore[no-untyped-def]
    """A caller in department A."""
    return RequestIdentity(tenant, USER, frozenset(roles), (DepartmentRole(DEPT_A, "lead"),))


def adapter(rows, tac4=None, cache=None, clock=None):  # type: ignore[no-untyped-def]
    """An adapter over a fake resolver, and its store."""
    r, store = resolver(rows, tac4, cache, clock)
    return CorpusScopeAdapter(r), store


async def code_of(awaitable) -> ErrorCode:  # type: ignore[no-untyped-def]
    """The §24 code a call raised."""
    with pytest.raises(DomainError) as info:
        await awaitable
    return info.value.code


async def test_iso5_empty_scope_fails_the_request_and_never_widens() -> None:
    """ISO-5: nothing resolved is 404 for a read, and an empty list for P-1."""
    a, _ = adapter([row("tender", departments=(uuid4(),))])
    assert (
        await code_of(a.resolve(identity("member"), ScopeSpec(), GrantLevel.SEARCH))
        is ErrorCode.COLLECTION_NOT_FOUND
    )
    assert await a.visible_collection_ids(identity("member")) == ()


async def test_iso6_a_cache_entry_is_a_whole_resolution() -> None:
    """ISO-6: a cached scope is never merged with a fresh partial one; a miss re-resolves fully."""
    clock, cache = Clock(), DictCache()
    first, second = row("tenant"), row("tender")
    r, store = resolver([first], cache=cache, clock=clock)
    a = CorpusScopeAdapter(r)
    one = await a.resolve(identity("member"), ScopeSpec(), GrantLevel.SEARCH)
    store.rows.append(second)  # a collection write we own: the next fresh resolution sees it
    cached = await a.resolve(identity("member"), ScopeSpec(), GrantLevel.SEARCH)
    assert cached.resolution_hash == one.resolution_hash and len(cached.collections) == 1
    clock.advance(61)
    fresh = await a.resolve(identity("member"), ScopeSpec(), GrantLevel.SEARCH)
    assert len(fresh.collections) == 2


async def test_iso7_reference_is_readable_by_any_tenant() -> None:
    """ISO-7 (read half): a PLATFORM reference collection resolves for two different tenants."""
    ref = row("reference")
    for tenant in (TENANT, OTHER_TENANT):
        a, _ = adapter([ref])
        scope = await a.resolve(identity(tenant=tenant), ScopeSpec(), GrantLevel.SEARCH)
        assert scope.collections[0].id == ref.id


async def test_iso15_unnamed_request_resolves_no_restricted_collection() -> None:
    """ISO-15: restricted grants are not swept in by an unnamed request."""
    restricted = row("tender", restricted=True)
    a, _ = adapter([restricted, row("tenant")], FakeTac4({restricted.tender_id: "approve"}))
    scope = await a.resolve(identity("member"), ScopeSpec(), GrantLevel.SEARCH)
    assert restricted.id not in {c.id for c in scope.collections}


async def test_iso16_tac4_down_503_for_named_restricted_only() -> None:
    """ISO-16: 503 SCOPE_STORE_UNAVAILABLE for the named restricted tender; others unaffected."""
    restricted = row("tender", restricted=True)
    tac4 = FakeTac4()
    tac4.error = True
    a, _ = adapter([restricted, row("tenant")], tac4)
    code = await code_of(
        a.resolve(identity("member"), ScopeSpec(tender_id=restricted.tender_id), GrantLevel.SEARCH)
    )
    assert code is ErrorCode.SCOPE_STORE_UNAVAILABLE
    assert await a.resolve(identity("member"), ScopeSpec(), GrantLevel.SEARCH)


async def test_iso18_revoked_grant_is_404_after_the_cache_bound() -> None:
    """ISO-18 / API-10: the authorizer re-resolves; a revoked overlay grant 404s after 60 s."""
    clock, cache = Clock(), DictCache()
    c = row("tender", restricted=True)
    tac4 = FakeTac4({c.tender_id: "edit"})
    a, _ = adapter([c], tac4, cache, clock)
    target = ScopeTarget(tender_id=c.tender_id)
    await a.require_level(identity("member"), target, GrantLevel.ANSWER)
    tac4.answers[c.tender_id] = None
    clock.advance(30)
    await a.require_level(identity("member"), target, GrantLevel.ANSWER)  # still inside the bound
    clock.advance(31)
    assert (
        await code_of(a.require_level(identity("member"), target, GrantLevel.ANSWER))
        is ErrorCode.COLLECTION_NOT_FOUND
    )


async def test_authorizer_403_only_for_a_held_grant_below_the_level() -> None:
    """403 is reserved for a held-but-insufficient grant; a reference corpus confers nothing."""
    t = row("tender", restricted=True)
    a, _ = adapter([t, row("reference")], FakeTac4({t.tender_id: "view"}))
    target = ScopeTarget(tender_id=t.tender_id)
    await a.require_level(identity(), target, GrantLevel.ANSWER)
    assert (
        await code_of(a.require_level(identity(), target, GrantLevel.GENERATE))
        is ErrorCode.GRANT_LEVEL_INSUFFICIENT
    )


async def test_adv5_foreign_tender_id_is_404_not_content() -> None:
    """ADV-5: a tender_id for an ungranted tender returns 404."""
    a, _ = adapter([row("tenant")])
    assert (
        await code_of(a.require_level(identity("member"), ScopeTarget(uuid4()), GrantLevel.SEARCH))
        is ErrorCode.COLLECTION_NOT_FOUND
    )


async def test_visible_ids_propagate_a_store_outage_as_503() -> None:
    """P-1 stays empty for no scope but refuses on an unreadable store (CG-4)."""
    a, store = adapter([row("tenant")])
    store.fail = 2
    assert (
        await code_of(a.visible_collection_ids(identity("member")))
        is ErrorCode.SCOPE_STORE_UNAVAILABLE
    )


async def test_visible_ids_with_only_returns_only_that_collection() -> None:
    """P-2/P-3: ``only`` narrows to the named collection or nothing."""
    t, other = row("tenant"), row("tender")
    a, _ = adapter([t, other])
    assert await a.visible_collection_ids(identity("member"), only=t.id) == (t.id,)
    assert await a.visible_collection_ids(identity("member"), only=uuid4()) == ()


async def test_resolved_scope_carries_the_token_and_the_levels() -> None:
    """The application view mirrors the token; the handle is the token itself."""
    c = row("tender", generation=None)
    a, _ = adapter([c])
    scope = await a.resolve(identity("member"), ScopeSpec(), GrantLevel.ANSWER)
    assert scope.handle.resolution_hash == scope.resolution_hash
    assert scope.empty_collection_ids == (c.id,) and scope.searchable == ()
    assert scope.level is GrantLevel.GENERATE and ScopeRequest().tender_id is None


async def test_iso18_a9_revoked_grant_looks_exactly_like_an_absent_artefact() -> None:
    """ISO-18 / CG-8 end to end: the real adapter behind A-9; same code and detail either way."""
    from tests.support_generation import TENANT as GEN_TENANT
    from tests.usecase.test_generation_queries import World

    from rag.application.generation.queries import GetSectionVersionContent

    w = World()
    restricted = row("tender", restricted=True, tender=w.art.tender_id, tenant=GEN_TENANT)
    tac4 = FakeTac4({restricted.tender_id: "edit"})
    clock = Clock()
    r, _ = resolver([restricted], tac4, DictCache(), clock)
    a9 = GetSectionVersionContent(w.tx, w.repo, CorpusScopeAdapter(r), w.store)
    who = identity("member", tenant=GEN_TENANT)
    ref = str(w.sec1.tender_section_ref)
    assert (await a9.execute(who, w.art.id, ref, 1)).content
    tac4.answers[restricted.tender_id] = None  # revoked in IAM
    clock.advance(61)
    with pytest.raises(DomainError) as revoked:
        await a9.execute(who, w.art.id, ref, 1)
    with pytest.raises(DomainError) as absent:
        await a9.execute(who, uuid4(), ref, 1)
    assert revoked.value.code is absent.value.code is ErrorCode.ARTEFACT_NOT_FOUND
    assert revoked.value.detail == absent.value.detail
