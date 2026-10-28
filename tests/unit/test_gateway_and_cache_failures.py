"""Gateway client failure paths, TAC-4 client, Valkey adapters: errors degrade, never leak."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any
from uuid import uuid4

import httpx
import pytest

from rag.application.shared.gateway import (
    GatewayContext,
    GatewayDeadlineExceeded,
    GatewayUnavailable,
)
from rag.infrastructure.cache.invalidation import ValkeyPlacementCache, ValkeyProfileCache
from rag.infrastructure.cache.valkey import ValkeyCache, build_cache
from rag.infrastructure.config import Settings
from rag.infrastructure.gateway.client import HttpModelGateway
from rag.infrastructure.iam import tac4

CTX = GatewayContext(plan="pro", residency_mode="standard", preferred_model=None)


def gw(handler: Any) -> HttpModelGateway:
    g = HttpModelGateway(Settings())
    g._client = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://gw.test")
    return g


def reply(body: Any, status: int = 200) -> Any:
    return lambda request: httpx.Response(status, json=body)


async def test_gateway_lazily_builds_and_closes_its_client() -> None:
    """The client is created on first use and ``aclose`` is safe either way."""
    g = HttpModelGateway(Settings())
    await g.aclose()  # nothing built yet
    assert g._http() is g._http()
    await g.aclose()


@pytest.mark.parametrize(
    ("exc", "expected"),
    [
        (httpx.ReadTimeout("slow"), GatewayDeadlineExceeded),
        (httpx.ConnectError("down"), GatewayUnavailable),
    ],
)
async def test_post_maps_transport_errors(exc: Exception, expected: type) -> None:
    """A timeout is a deadline; any other transport error is unavailable."""

    def handler(request: httpx.Request) -> httpx.Response:
        raise exc

    with pytest.raises(expected):
        await gw(handler).rerank("q", ["a"], ctx=CTX, deadline_seconds=1)


async def test_post_rejects_a_non_json_or_non_object_body() -> None:
    """Malformed bodies are an outage, not a crash."""
    bad_json = gw(lambda r: httpx.Response(200, content=b"not json"))
    with pytest.raises(GatewayUnavailable):
        await bad_json.rerank("q", ["a"], ctx=CTX, deadline_seconds=1)
    not_object = gw(reply([1, 2]))
    with pytest.raises(GatewayUnavailable):
        await not_object.rerank("q", ["a"], ctx=CTX, deadline_seconds=1)


async def test_embed_rerank_complete_reject_malformed_payloads() -> None:
    """Each endpoint's body shape is checked."""
    with pytest.raises(GatewayUnavailable):
        await gw(reply({"data": [{"nope": 1}]})).embed(["t"], ctx=CTX)
    with pytest.raises(GatewayUnavailable):
        await gw(reply({"results": [{"index": 9, "relevance_score": 1}]})).rerank(
            "q", ["a"], ctx=CTX, deadline_seconds=1
        )
    with pytest.raises(GatewayUnavailable):
        await gw(reply({"choices": []})).complete("judge", "s", "u", ctx=CTX, deadline_seconds=1)


async def test_count_estimates_when_tokenize_fails() -> None:
    """No tokenizer: three characters per token, rounded up, and the call never fails."""
    assert await gw(reply({}, 503)).count(["abcdefg", ""], ctx=CTX) == [3, 0]
    assert await gw(reply({"count": 4})).count(["abcdefg"], ctx=CTX) == [4]


async def test_stream_maps_timeouts_and_transport_errors() -> None:
    """Both stream failure types surface as gateway errors on first iteration."""

    def timeout(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow")

    def down(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("down")

    async def drain(g: HttpModelGateway) -> None:
        stream: AsyncIterator[Any] = g.generate_stream(
            "draft", "s", "u", ctx=CTX, deadline_seconds=1
        )
        async for _ in stream:
            pass

    with pytest.raises(GatewayDeadlineExceeded):
        await drain(gw(timeout))
    with pytest.raises(GatewayUnavailable):
        await drain(gw(down))


# ---- TAC-4 ------------------------------------------------------------------------------


def _tac4(handler: Any, monkeypatch: pytest.MonkeyPatch) -> tac4.HttpTenderAccess:
    monkeypatch.setattr(
        tac4,
        "create_platform_http_client",
        lambda **kw: httpx.AsyncClient(
            transport=httpx.MockTransport(handler), base_url="http://iam.test"
        ),
    )
    return tac4.HttpTenderAccess("http://iam.test")


async def test_tac4_returns_the_level(monkeypatch: pytest.MonkeyPatch) -> None:
    """A level string, or null for a successful no-access answer."""
    ids = (uuid4(), uuid4(), uuid4())
    assert await _tac4(reply({"level": "edit"}), monkeypatch).level(*ids) == "edit"
    assert await _tac4(reply({"level": None}), monkeypatch).level(*ids) is None


async def test_tac4_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    """A non-200 or a non-string level raises so the resolver refuses (CG-4)."""
    ids = (uuid4(), uuid4(), uuid4())
    with pytest.raises(httpx.HTTPStatusError):
        await _tac4(reply({}, 503), monkeypatch).level(*ids)
    with pytest.raises(ValueError, match="not a string"):
        await _tac4(reply({"level": 3}), monkeypatch).level(*ids)
    with pytest.raises(RuntimeError, match="not configured"):
        await tac4.UnconfiguredTenderAccess().level(*ids)


# ---- Valkey -----------------------------------------------------------------------------


class FakeValkey:
    def __init__(self, keys: list[Any]) -> None:
        self.keys, self.deleted = keys, []

    async def scan_iter(self, match: str, count: int = 0) -> AsyncIterator[Any]:
        for k in self.keys:
            yield k

    async def delete(self, *keys: str) -> None:
        self.deleted.extend(keys)


async def test_profile_cache_drops_matching_keys_and_reports_the_version() -> None:
    """Bytes and str keys are decoded; the last segment of the last key is the version."""
    t = uuid4()
    client = FakeValkey([f"{t}:pack:d:l:c:v1".encode(), f"{t}:pack:d:l:c:v2"])
    cache = ValkeyProfileCache(client)
    assert await cache.invalidate(t, "d", "l", "c") == "v2"
    assert client.deleted == [f"{t}:pack:d:l:c:v1", f"{t}:pack:d:l:c:v2"]
    empty = FakeValkey([])
    assert await ValkeyProfileCache(empty).invalidate(t, "d", "l", "c") is None
    assert empty.deleted == []


async def test_placement_cache_deletes_the_one_key() -> None:
    """``DEL {tenant}:coll:{id}``."""
    t, c = uuid4(), uuid4()
    client = FakeValkey([])
    await ValkeyPlacementCache(client).evict(t, c)
    assert client.deleted == [f"{t}:coll:{c}"]


class BrokenRedis:
    async def get(self, key: str) -> Any:
        raise ConnectionError

    async def set(self, *a: Any, **k: Any) -> None:
        raise ConnectionError

    async def aclose(self) -> None:
        raise ConnectionError


async def test_valkey_cache_swallows_every_failure() -> None:
    """An outage is a miss (§11.4): get returns None, set and close do not raise."""
    cache = ValkeyCache("redis://localhost:1/0")
    cache._client = BrokenRedis()  # type: ignore[assignment]
    assert await cache.get("k") is None
    await cache.set("k", "v", 5)
    await cache.aclose()


async def test_valkey_cache_get_ignores_non_string_values() -> None:
    """Only strings count as hits."""

    class Odd:
        async def get(self, key: str) -> Any:
            return 7

    cache = ValkeyCache("redis://localhost:1/0")
    cache._client = Odd()  # type: ignore[assignment]
    assert await cache.get("k") is None


def test_build_cache_is_none_without_a_url() -> None:
    """No URL, no cache: callers bypass to source."""
    assert build_cache(Settings(valkey_url="")) is None
    assert isinstance(build_cache(Settings(valkey_url="redis://localhost:1/0")), ValkeyCache)


async def test_every_call_carries_the_tenant_for_the_gateways_per_tenant_budget() -> None:
    """``x-tenant-id`` is the key the Gateway's token budget is charged to; without it a call
    counts against no tenant."""
    tenant = uuid4()
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(
            200,
            json={
                "data": [{"embedding": [1.0]}],
                "results": [{"index": 0, "relevance_score": 0.5}],
                "choices": [{"message": {"content": "x"}}],
            },
        )

    ctx = GatewayContext(plan="pro", residency_mode="standard", tenant_id=tenant)
    g = gw(handler)
    await g.embed(["t"], ctx=ctx)
    await g.rerank("q", ["a"], ctx=ctx, deadline_seconds=1)
    await g.complete("judge", "s", "u", ctx=ctx, deadline_seconds=1)
    assert [r.headers["x-tenant-id"] for r in seen] == [str(tenant)] * 3
    # no tenant, no header: the context is optional for calls made on the platform's behalf
    seen.clear()
    await g.embed(["t"], ctx=CTX)
    assert "x-tenant-id" not in seen[0].headers


async def test_rerank_uses_the_cohere_v2_path_the_gateway_serves() -> None:
    """Agent Router exposes rerank at ``/cohere/v2/rerank``, not ``/v1/rerank``."""
    paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        return httpx.Response(200, json={"results": [{"index": 1, "relevance_score": 0.9}]})

    scores = await gw(handler).rerank("q", ["a", "b"], ctx=CTX, deadline_seconds=1)
    assert paths == ["/cohere/v2/rerank"]
    assert scores == [0.0, 0.9]
