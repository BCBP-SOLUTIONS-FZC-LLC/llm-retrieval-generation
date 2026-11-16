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
    granted = {"has_access": True, "access_level": "edit", "expires_at": None}
    assert await _tac4(reply(granted), monkeypatch).level(*ids) == "edit"
    none = {"has_access": False, "access_level": None}
    assert await _tac4(reply(none), monkeypatch).level(*ids) is None


async def test_tac4_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    """A non-200 or a non-string level raises so the resolver refuses (CG-4)."""
    ids = (uuid4(), uuid4(), uuid4())
    with pytest.raises(httpx.HTTPStatusError):
        await _tac4(reply({}, 503), monkeypatch).level(*ids)
    with pytest.raises(ValueError, match="not a string"):
        await _tac4(reply({"has_access": True, "access_level": 3}), monkeypatch).level(*ids)
    with pytest.raises(ValueError, match="not a boolean"):
        await _tac4(reply({"level": "edit"}), monkeypatch).level(*ids)
    with pytest.raises(RuntimeError, match="not configured"):
        await tac4.UnconfiguredTenderAccess().level(*ids)


async def test_tac4_logs_an_invalid_body_with_the_status_and_no_content(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A malformed IAM answer was a bare 503 with no line saying IAM's answer was the problem."""
    seen: list[tuple[str, dict[str, Any]]] = []

    class Rec:
        def warning(self, name: str, **kw: Any) -> None:
            seen.append((name, kw))

    monkeypatch.setattr(tac4, "log", Rec())
    ids = (uuid4(), uuid4(), uuid4())
    bodies = ({"level": "secret-ish"}, {"has_access": True, "access_level": 3})
    for body in bodies:
        with pytest.raises(ValueError):
            await _tac4(reply(body), monkeypatch).level(*ids)
    not_json = httpx.Response(200, content=b"<html>secret</html>")
    with pytest.raises(ValueError):
        await _tac4(lambda r: not_json, monkeypatch).level(*ids)
    assert [(n, kw["why"]) for n, kw in seen] == [
        ("tac4_invalid_body", "has_access"),
        ("tac4_invalid_body", "access_level"),
        ("tac4_invalid_body", "not_json"),
    ]
    assert all(set(kw) == {"status", "why"} for _, kw in seen)  # nothing from the body


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


async def test_valkey_outage_is_logged_once_and_the_recovery_too(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Fail-open stays, but it is no longer silent: one line per outage, no key, no value."""
    from rag.infrastructure.cache import valkey

    events: list[tuple[str, dict[str, Any]]] = []

    class Rec:
        def warning(self, name: str, **kw: Any) -> None:
            events.append((name, kw))

        def info(self, name: str, **kw: Any) -> None:
            events.append((name, kw))

    monkeypatch.setattr(valkey, "log", Rec())
    cache = ValkeyCache("redis://localhost:1/0")
    cache._client = BrokenRedis()  # type: ignore[assignment]
    for _ in range(3):
        await cache.get("secret-key")
    await cache.set("secret-key", "secret-value", 5)
    assert events == [("cache_unavailable", {"op": "get", "error_type": "ConnectionError"})]

    class Healthy:
        async def get(self, key: str) -> Any:
            return None

    cache._client = Healthy()  # type: ignore[assignment]
    await cache.get("k")
    assert events[-1] == ("cache_recovered", {})


async def test_a_flapping_valkey_logs_one_outage_a_minute_not_one_per_flap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from rag.infrastructure.cache import valkey

    events: list[str] = []

    class Rec:
        def warning(self, name: str, **kw: Any) -> None:
            events.append(name)

        def info(self, name: str, **kw: Any) -> None:
            events.append(name)

    class Healthy:
        async def get(self, key: str) -> Any:
            return None

    clock = [1000.0]
    monkeypatch.setattr(valkey, "log", Rec())
    monkeypatch.setattr(valkey.time, "monotonic", lambda: clock[0])
    cache = ValkeyCache("redis://localhost:1/0")
    for _ in range(3):  # three flaps inside one minute
        cache._client = BrokenRedis()  # type: ignore[assignment]
        await cache.get("k")
        cache._client = Healthy()  # type: ignore[assignment]
        await cache.get("k")
        clock[0] += 5
    assert events == ["cache_unavailable", "cache_recovered"]  # the later flaps stay quiet
    clock[0] += 120
    cache._client = BrokenRedis()  # type: ignore[assignment]
    await cache.get("k")
    assert events[-1] == "cache_unavailable"


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
        results = [{"index": 1, "relevance_score": 0.9}, {"index": 0, "relevance_score": 0.4}]
        return httpx.Response(200, json={"results": results})

    scores = await gw(handler).rerank("q", ["a", "b"], ctx=CTX, deadline_seconds=1)
    assert paths == ["/cohere/v2/rerank"]
    assert scores == [0.4, 0.9]
