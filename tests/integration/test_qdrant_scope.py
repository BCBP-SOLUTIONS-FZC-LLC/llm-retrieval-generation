"""Scope filters against a real Qdrant (v1.14.1, as compose): isolation, RET-8 and the compound.

The unit tests prove the filter is *built* right; this proves Qdrant *honours* it. One physical
collection with custom sharding is shared by two tenants, as in production (SCOPE-5). Custom
shard keys need Qdrant's cluster mode, which the compose service does not enable, so this starts
its own container. Needs Docker, or ``RAG_TEST_QDRANT_URL`` for a cluster-mode server; skipped
by default.
"""

from __future__ import annotations

import os
import urllib.request
from collections.abc import AsyncIterator, Iterator
from dataclasses import replace
from uuid import uuid4

import pytest
from qdrant_client import AsyncQdrantClient, models
from tests.support_scope import OTHER_TENANT, T0, TENANT, principal, resolver, row

from rag.corpusscope import FilterError, QdrantScopedReader, ScopeRequest, ScopeToken

pytestmark = pytest.mark.integration

PHYSICAL = "scope_test"
VECTOR = [1.0, 0.0, 0.0, 0.0]


@pytest.fixture(scope="module")
def qdrant_url() -> Iterator[str]:
    """A running server's URL, or a throwaway container."""
    if url := os.environ.get("RAG_TEST_QDRANT_URL"):
        yield url
        return
    pytest.importorskip("testcontainers.core.container")
    from testcontainers.core.container import DockerContainer
    from testcontainers.core.waiting_utils import wait_for_logs

    image = DockerContainer("qdrant/qdrant:v1.14.1").with_env("QDRANT__CLUSTER__ENABLED", "true")
    image = image.with_command("./entrypoint.sh --uri http://127.0.0.1:6335")
    with image.with_exposed_ports(6333) as container:
        wait_for_logs(container, "Qdrant HTTP listening", timeout=60)
        url = f"http://{container.get_container_host_ip()}:{container.get_exposed_port(6333)}"
        urllib.request.urlopen(f"{url}/readyz", timeout=10).read()
        yield url


def _point(coll, *, tenant=None, generation=None, current=True, role="specification", lang="en-GB"):  # type: ignore[no-untyped-def]
    return models.PointStruct(
        id=str(uuid4()),
        vector=VECTOR,
        payload={
            "tenant_id": str(tenant or coll.tenant_id),
            "tender_id": str(coll.tender_id),
            "collection_id": str(coll.id),
            "index_generation": generation or coll.active_generation,
            "is_current": current,
            "document_role": role,
            "language": lang,
            "entity_types": ["deadline"],
        },
    )


@pytest.fixture
async def world(qdrant_url: str) -> AsyncIterator[dict]:  # type: ignore[type-arg]
    """Tenant A's collection, tenant B's collection, and points that tempt a leak."""
    mine = replace(row("tender", generation=2), physical_collection=PHYSICAL)
    theirs = replace(row("tender", tenant=OTHER_TENANT, generation=1), physical_collection=PHYSICAL)
    client = AsyncQdrantClient(url=qdrant_url, check_compatibility=False)
    if await client.collection_exists(PHYSICAL):
        await client.delete_collection(PHYSICAL)
    await client.create_collection(
        PHYSICAL,
        vectors_config=models.VectorParams(size=4, distance=models.Distance.COSINE),
        sharding_method=models.ShardingMethod.CUSTOM,
    )
    for coll in (mine, theirs):
        await client.create_shard_key(PHYSICAL, coll.shard_key)
    own = [_point(mine), _point(mine, role="drawing")]
    stale = [
        _point(mine, current=False),  # superseded version: RET-8
        _point(mine, generation=1),  # the retired generation: IG-5
    ]
    # Tenant B's points sit in *my* shard: only the payload compound keeps them out.
    foreign_in_my_shard = [_point(mine, tenant=OTHER_TENANT)]
    other = [_point(theirs)]
    await client.upsert(
        PHYSICAL, own + stale + foreign_in_my_shard, shard_key_selector=mine.shard_key
    )
    await client.upsert(PHYSICAL, other, shard_key_selector=theirs.shard_key)

    mine_resolver, _ = resolver([mine])
    token_a: ScopeToken = await mine_resolver.resolve(principal("member"), ScopeRequest())
    theirs_resolver, _ = resolver([theirs])
    token_b: ScopeToken = await theirs_resolver.resolve(
        principal("member", tenant=OTHER_TENANT), ScopeRequest()
    )
    reader = QdrantScopedReader(client)
    yield {
        "reader": reader,
        "mine": mine,
        "theirs": theirs,
        "token_a": token_a,
        "token_b": token_b,
        "own_ids": {p.id for p in own},
        "other_ids": {p.id for p in other},
    }
    await client.delete_collection(PHYSICAL)
    await reader.aclose()


async def test_search_returns_only_my_current_points_of_the_active_generation(world) -> None:  # type: ignore[no-untyped-def]
    """SCOPE-2 + RET-8: not stale, not the retired generation, not another tenant's."""
    hits = await world["reader"].dense_search(
        world["token_a"], world["mine"].id, VECTOR, 50, now=T0
    )
    assert {h.point_id for h in hits} == world["own_ids"]
    assert all(h.payload["tenant_id"] == str(TENANT) for h in hits)


async def test_the_other_tenant_sees_only_its_own_point(world) -> None:  # type: ignore[no-untyped-def]
    """The same vector, the same physical collection, the other tenant's token."""
    hits = await world["reader"].dense_search(
        world["token_b"], world["theirs"].id, VECTOR, 50, now=T0
    )
    assert {h.point_id for h in hits} == world["other_ids"]


async def test_a_token_cannot_name_a_collection_it_was_not_issued_for(world) -> None:  # type: ignore[no-untyped-def]
    """ISO-3: tenant A's token cannot reach tenant B's collection by id, so no query is sent."""
    with pytest.raises((FilterError, KeyError, LookupError)):
        await world["reader"].dense_search(world["token_a"], world["theirs"].id, VECTOR, 50, now=T0)


async def test_structural_filters_narrow_inside_the_scope(world) -> None:  # type: ignore[no-untyped-def]
    """Roles and language narrow the result; they never widen it past the scope."""
    only_drawing = await world["reader"].dense_search(
        world["token_a"], world["mine"].id, VECTOR, 50, now=T0, document_roles=("drawing",)
    )
    assert len(only_drawing) == 1 and only_drawing[0].payload["document_role"] == "drawing"
    english = await world["reader"].dense_search(
        world["token_a"], world["mine"].id, VECTOR, 50, now=T0, language="en"
    )
    assert {h.point_id for h in english} == world["own_ids"]
    german = await world["reader"].dense_search(
        world["token_a"], world["mine"].id, VECTOR, 50, now=T0, language="de"
    )
    assert german == []


async def test_count_is_exact_and_scoped(world) -> None:  # type: ignore[no-untyped-def]
    """§6.9: the required-input count sees current points of my collection only."""
    r, c = world["reader"], world["mine"]
    assert await r.count(world["token_a"], c.id, now=T0) == 2
    assert await r.count(world["token_a"], c.id, now=T0, document_role="drawing") == 1
    assert await r.count(world["token_a"], c.id, now=T0, entity_type="deadline") == 2
    assert await r.count(world["token_a"], c.id, now=T0, entity_type="nothing") == 0
