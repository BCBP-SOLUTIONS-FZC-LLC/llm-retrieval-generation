"""Filter construction (SCOPE-1, SCOPE-2, MD-08): none exists without the compound."""

from __future__ import annotations

import inspect
from dataclasses import replace
from datetime import timedelta
from uuid import uuid4

import pytest
from qdrant_client import models
from tests.support_scope import OTHER_TENANT, T0, TENANT, principal, resolver, row

from rag.corpusscope import (
    PLATFORM,
    REFERENCE,
    TENANT_WIDE,
    FilterError,
    Level,
    ScopeExpired,
    ScopeRequest,
    ScopeToken,
    count_filter,
    delete_filter,
    scroll_filter,
    search_filter,
)
from rag.corpusscope import filters as filters_module
from rag.corpusscope.token import ScopedCollection

MANDATORY = {"tenant_id", "tender_id", "collection_id", "index_generation"}


def keys(flt: models.Filter) -> dict[str, object]:
    """``{key: match value}`` of a filter's ``must`` terms."""
    out: dict[str, object] = {}
    for cond in flt.must or []:
        assert isinstance(cond, models.FieldCondition)
        match = cond.match
        out[cond.key] = getattr(match, "value", getattr(match, "any", getattr(match, "text", None)))
    return out


async def token_for(*rows) -> ScopeToken:  # type: ignore[no-untyped-def]
    """A live token over ``rows`` for a member."""
    r, _ = resolver(list(rows))
    return await r.resolve(principal("member"), ScopeRequest())


async def test_every_filter_kind_carries_the_whole_compound() -> None:
    """ISO-1/ISO-2: search, count, delete and scroll carry the whole mandatory compound."""
    c = row("tender", generation=4)
    token = await token_for(c)
    built = [
        search_filter(token, c.id, now=T0),
        count_filter(token, c.id, now=T0, entity_type="standard"),
        delete_filter(token, c.id, now=T0, document_id=uuid4()),
        scroll_filter(token, c.id, now=T0),
    ]
    for flt in built:
        k = keys(flt)
        assert MANDATORY <= set(k)
        assert k["tenant_id"] == str(TENANT) and k["tender_id"] == str(c.tender_id)
        assert k["collection_id"] == str(c.id) and k["index_generation"] == 4


async def test_sentinels_per_collection_type() -> None:
    """§4.10: tenant -> TENANT_WIDE, reference -> PLATFORM + REFERENCE, canonical lower case."""
    t, ref = row("tenant"), row("reference")
    token = await token_for(t, ref)
    kt, kr = keys(search_filter(token, t.id, now=T0)), keys(search_filter(token, ref.id, now=T0))
    assert kt["tender_id"] == str(TENANT_WIDE) and kt["tenant_id"] == str(TENANT)
    assert kr["tender_id"] == str(REFERENCE) and kr["tenant_id"] == str(PLATFORM)
    assert all(v == str(v).lower() for v in (kt["tender_id"], kr["tenant_id"]))


async def test_is_current_on_search_and_count_but_not_on_delete() -> None:
    """RET-8: a server-side predicate no caller can drop; a delete must reach superseded points."""
    c = row("tender")
    token = await token_for(c)
    assert keys(search_filter(token, c.id, now=T0))["is_current"] is True
    assert keys(count_filter(token, c.id, now=T0, entity_type="x"))["is_current"] is True
    assert "is_current" not in keys(delete_filter(token, c.id, now=T0))
    for fn in (search_filter, count_filter):
        assert "include_superseded" not in inspect.signature(fn).parameters
        assert not any("current" in p for p in inspect.signature(fn).parameters)


async def test_structural_predicates_are_additive_never_replacing() -> None:
    """Roles, language and entity narrow the compound; they never stand in for it."""
    c = row("tender")
    token = await token_for(c)
    k = keys(
        search_filter(
            token,
            c.id,
            now=T0,
            document_roles=("specification",),
            language="en-GB",
            entity_type="standard",
        )
    )
    assert MANDATORY <= set(k) and k["language"] == "en"  # primary subtag (§4.10)
    assert k["document_role"] == ["specification"] and k["entity_types"] == "standard"


async def test_filters_require_a_scope_token() -> None:
    """ISO-3: not a token, no filter (type rule; runtime backstop)."""
    c = row("tender")
    token = await token_for(c)
    with pytest.raises(TypeError):
        search_filter(object(), c.id, now=T0)  # type: ignore[arg-type]
    with pytest.raises(FilterError):
        search_filter(token, uuid4(), now=T0)  # a collection the token does not carry


async def test_expired_token_cannot_construct_a_filter() -> None:
    """ISO-4 / SCOPE-3: nothing extends a token."""
    c = row("tender")
    token = await token_for(c)
    with pytest.raises(ScopeExpired):
        search_filter(token, c.id, now=token.expires_at)
    with pytest.raises(ScopeExpired):
        delete_filter(token, c.id, now=token.expires_at + timedelta(seconds=1))


async def test_empty_corpus_builds_no_filter() -> None:
    """SCOPE-12: no generation filter is constructed against an absent generation."""
    c = row("tender", generation=None)
    token = await token_for(c)
    with pytest.raises(FilterError):
        search_filter(token, c.id, now=T0)


def test_cross_tenant_token_cannot_exist_for_another_tenant() -> None:
    """ISO-1: a token for tenant A refuses a collection stamped with tenant B."""
    foreign = ScopedCollection(
        uuid4(),
        "tender",
        Level.SEARCH,
        __import__("decimal").Decimal("1.00"),
        "p",
        "s",
        OTHER_TENANT,
        uuid4(),
        1,
        0,
    )
    with pytest.raises(Exception, match="another tenant"):
        ScopeToken.create(
            tenant_id=TENANT,
            principal_id=None,
            collections=(foreign,),
            level=Level.SEARCH,
            clamped=False,
            resolved_at=T0,
            expires_at=T0 + timedelta(seconds=60),
        )


def test_collection_identity_must_follow_the_sentinel_rules() -> None:
    """SCOPE-2: null/empty/inconsistent tenant or tender raises at construction."""
    from decimal import Decimal

    base = dict(
        id=uuid4(),
        level=Level.SEARCH,
        weight=Decimal("1.00"),
        physical_collection="p",
        shard_key="s",
        index_generation=1,
        purge_epoch=0,
    )
    with pytest.raises(FilterError):  # reference without the PLATFORM tenant
        ScopedCollection(type="reference", tenant_id=TENANT, tender_id=REFERENCE, **base)  # type: ignore[arg-type]
    with pytest.raises(FilterError):  # tenant collection with a real tender id
        ScopedCollection(type="tenant", tenant_id=TENANT, tender_id=uuid4(), **base)  # type: ignore[arg-type]
    with pytest.raises(FilterError):  # tender collection on a sentinel
        ScopedCollection(type="tender", tenant_id=TENANT, tender_id=TENANT_WIDE, **base)  # type: ignore[arg-type]
    with pytest.raises(FilterError):  # empty placement
        ScopedCollection(
            type="tender", tenant_id=TENANT, tender_id=uuid4(), **{**base, "shard_key": ""}
        )  # type: ignore[arg-type]


async def test_tampered_collection_is_not_the_tokens() -> None:
    """A collection value that differs from the token's (e.g. another generation) is refused."""
    c = row("tender")
    token = await token_for(c)
    other = replace(token.collections[0], index_generation=99)
    with pytest.raises(FilterError):
        filters_module.mandatory_conditions(token, other)


def test_mandatory_terms_are_built_in_one_place() -> None:
    """No other function in the module assembles a Filter without ``mandatory_conditions``."""
    src = inspect.getsource(filters_module)
    assert src.count("models.Filter(") == 4  # search, count, delete, scroll
    assert src.count("mandatory_conditions(") >= 5  # the definition + four uses
