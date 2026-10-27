"""Infrastructure adapters without a network: Gateway, packs, breaker, lexical SQL, metrics."""

from __future__ import annotations

import json
from decimal import Decimal
from uuid import UUID, uuid4

import httpx
import pytest
from fastapicommon.metrics import default_registry
from tests.support_retrieval import DictKV, bundle

from rag.application.shared.gateway import (
    GatewayContext,
    GatewayDeadlineExceeded,
    GatewayQuotaRefused,
    GatewayResidencyRefused,
    GatewayUnavailable,
)
from rag.corpusscope import DenseHit
from rag.domain.packs.profile import PackUnavailable
from rag.infrastructure.config import Settings
from rag.infrastructure.gateway.client import HttpModelGateway
from rag.infrastructure.observability.metrics import (
    METRIC_NAMES,
    P,
    build_observer,
    depth_band,
)
from rag.infrastructure.resilience import BreakerOpen, CircuitBreaker
from rag.infrastructure.retrieval.lexical import PgLexicalSearch, build_sql
from rag.infrastructure.retrieval.qdrant import to_candidate
from rag.infrastructure.tender.pack_codec import bundle_from_dict, bundle_to_dict
from rag.infrastructure.tender.packs import HttpPackResolver, ValkeyPackCache

CTX = GatewayContext(plan="pro", residency_mode="strict", preferred_model="m1")
TENANT = UUID("0a3f0000-0000-7000-8000-000000000001")


def gateway(handler) -> HttpModelGateway:  # type: ignore[no-untyped-def]
    """A gateway whose HTTP client is a mock transport."""
    gw = HttpModelGateway(Settings())
    gw._client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="http://gw.test"
    )
    return gw


# --- Gateway -----------------------------------------------------------------------------------


async def test_embed_posts_the_task_class_and_routing_headers() -> None:
    """§22.1: the task class is the virtual model; plan, residency and hint ride as headers."""
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"data": [{"embedding": [1, 2.5]}]})

    assert await gateway(handler).embed(["q"], ctx=CTX) == [[1.0, 2.5]]
    r = seen[0]
    assert r.url.path == "/v1/embeddings" and json.loads(r.content)["model"] == "embed"
    assert (r.headers["x-plan"], r.headers["x-residency-mode"], r.headers["x-preferred-model"]) == (
        "pro",
        "strict",
        "m1",
    )


async def test_embed_retries_once_immediately_and_opens_the_breaker_after_five_failures() -> None:
    """§14.3: 1 immediate retry; 5 consecutive failures open the breaker (fast failure)."""
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(500)

    gw = gateway(handler)
    for _ in range(5):
        with pytest.raises(GatewayUnavailable):
            await gw.embed(["q"], ctx=CTX)
    assert calls["n"] == 10  # two attempts per call
    with pytest.raises(GatewayUnavailable, match="breaker"):
        await gw.embed(["q"], ctx=CTX)
    assert calls["n"] == 10  # the open breaker made no call


async def test_embed_does_not_retry_a_refusal() -> None:
    """A quota answer is an answer; retrying it spends the budget."""
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(429, headers={"retry-after": "12"})

    with pytest.raises(GatewayQuotaRefused) as e:
        await gateway(handler).embed(["q"], ctx=CTX)
    assert calls["n"] == 1 and e.value.retry_after_seconds == 12


async def test_rerank_maps_scores_by_index_and_never_retries() -> None:
    """§6.7: no retry; scores come back in input order."""
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(
            200,
            json={
                "results": [
                    {"index": 1, "relevance_score": 0.9},
                    {"index": 0, "relevance_score": 0.2},
                ]
            },
        )

    assert await gateway(handler).rerank("q", ["a", "b"], ctx=CTX, deadline_seconds=0.09) == [
        0.2,
        0.9,
    ]
    assert calls["n"] == 1

    def failing(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(503)

    with pytest.raises(GatewayUnavailable):
        await gateway(failing).rerank("q", ["a"], ctx=CTX, deadline_seconds=0.09)
    assert calls["n"] == 2  # exactly one more: no retry


async def test_timeouts_and_refusals_map_to_the_gateway_error_taxonomy() -> None:
    """Deadline, quota, residency and everything else are distinct reasons (RET-1, LD-99)."""

    def timeout(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow")

    with pytest.raises(GatewayDeadlineExceeded):
        await gateway(timeout).complete("extract", "s", "u", ctx=CTX, deadline_seconds=1)

    def residency(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, json={"error": {"code": "residency_capacity"}})

    with pytest.raises(GatewayResidencyRefused):
        await gateway(residency).complete("extract", "s", "u", ctx=CTX, deadline_seconds=1)

    def garbage(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="<html>")

    with pytest.raises(GatewayUnavailable):
        await gateway(garbage).complete("extract", "s", "u", ctx=CTX, deadline_seconds=1)


async def test_complete_asks_for_temperature_zero_json_on_the_task_class() -> None:
    """§22.1: refine-query / extract run at temperature 0 as JSON."""
    seen: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content))
        return httpx.Response(
            200, json={"model": "mp", "choices": [{"message": {"content": '{"a":1}'}}]}
        )

    out = await gateway(handler).complete(
        "refine-query", "sys", "usr", ctx=CTX, deadline_seconds=0.15
    )
    assert out.text == '{"a":1}' and out.model == "mp"
    assert seen[0]["model"] == "refine-query" and seen[0]["temperature"] == 0


async def test_token_counter_uses_the_gateway_and_over_counts_when_it_cannot() -> None:
    """§6.8: a conservative estimate (never fewer tokens than the text likely holds) on failure."""

    def ok(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"count": 7})

    assert await gateway(ok).count(["abc", "defg"], ctx=CTX) == [7, 7]

    def down(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500)

    assert await gateway(down).count(["abcdef", ""], ctx=CTX) == [2, 0]


# --- breaker -----------------------------------------------------------------------------------


async def test_breaker_opens_half_opens_and_closes() -> None:
    """5 failures open it; after the pause one probe decides."""
    now = [0.0]
    b = CircuitBreaker(3, 30.0, clock=lambda: now[0])

    async def boom() -> int:
        raise RuntimeError

    async def fine() -> int:
        return 1

    for _ in range(3):
        with pytest.raises(RuntimeError):
            await b.call(boom)
    with pytest.raises(BreakerOpen):
        await b.call(fine)
    now[0] = 31.0
    with pytest.raises(RuntimeError):  # failed probe re-opens for another pause
        await b.call(boom)
    with pytest.raises(BreakerOpen):
        await b.call(fine)
    now[0] = 62.0
    assert await b.call(fine) == 1 and not b.is_open


# --- packs -------------------------------------------------------------------------------------


def test_pack_codec_round_trips() -> None:
    """The cache's storage form is the wire shape."""
    original = bundle()
    assert bundle_from_dict(bundle_to_dict(original)) == original


async def test_pack_cache_is_tenant_prefixed_and_keyed_by_version() -> None:
    """§11.1: ``{tenant}:pack:{identity}:{version}``; a pinned read hits after any read of it."""
    kv = DictKV()
    cache = ValkeyPackCache(kv)
    b = bundle()
    await cache.put(TENANT, "dp-civil:en", None, b)
    assert set(kv.data) == {
        f"{TENANT}:pack:dp-civil:en:current",
        f"{TENANT}:pack:dp-civil:en:dp-1",
    }
    assert await cache.get(TENANT, "dp-civil:en", "dp-1") == b
    assert await cache.get(uuid4(), "dp-civil:en", "dp-1") is None  # never across tenants
    kv.data[f"{TENANT}:pack:dp-civil:en:current"] = "{bad"
    assert await cache.get(TENANT, "dp-civil:en", None) is None  # corrupt = miss


def resolver(handler, cache=None) -> HttpPackResolver:  # type: ignore[no-untyped-def]
    """A pack resolver over a mock transport with no backoff."""
    r = HttpPackResolver("http://tender.test", cache, backoff=(0.0, 0.0))
    r._client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="http://tender.test"
    )
    return r


async def test_pack_resolution_retries_twice_then_is_unavailable() -> None:
    """§14.3: 2 retries with backoff, then ``PackUnavailable`` (the caller falls to default)."""
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(502)

    with pytest.raises(PackUnavailable):
        await resolver(handler).resolve(TENANT, domain_profile_ref="dp-civil")
    assert calls["n"] == 3


async def test_pack_resolution_caches_and_a_hit_never_leaves_the_process() -> None:
    """RET-3: cache-fronted; the second resolve makes no call."""
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        assert request.headers["x-tenant-id"] == str(TENANT)
        return httpx.Response(200, json=bundle_to_dict(bundle()))

    r = resolver(handler, ValkeyPackCache(DictKV()))
    first = await r.resolve(TENANT, domain_profile_ref="dp-civil", language="en")
    second = await r.resolve(TENANT, domain_profile_ref="dp-civil", language="en")
    assert first == second and calls["n"] == 1
    assert (
        await r.resolve(TENANT, domain_profile_ref="dp-civil", language="en", pinned_version="dp-1")
    ) == first
    assert calls["n"] == 1  # the pinned read hits the version key


async def test_an_unreadable_pack_is_unavailable_not_a_crash() -> None:
    """A malformed Tender body degrades to the default profile."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"domain_profile": {}})

    with pytest.raises(PackUnavailable):
        await resolver(handler).resolve(TENANT)


# --- search adapters ---------------------------------------------------------------------------


def test_lexical_sql_numbers_its_parameters_densely() -> None:
    """No unused ``$n`` (asyncpg refuses it) whichever optional predicates apply."""
    for roles in (False, True):
        for lang in (False, True):
            sql = build_sql("document_chunks", with_roles=roles, with_language=lang)
            last = 5 + roles + lang + 1
            assert f"LIMIT ${last}" in sql and f"${last + 1}" not in sql
            assert ("document_role = ANY($6" in sql) is roles
            assert "c.is_current" in sql and "c.tenant_id = s.tenant_id" in sql


def test_lexical_sql_carries_the_whole_compound_and_is_read_only() -> None:
    """SCOPE-2 on the lexical half: tenant leads, then tender, collection, generation."""
    sql = build_sql("document_chunks", with_roles=False, with_language=False)
    on = sql[sql.index("ON c.tenant_id") : sql.index("websearch")]
    assert (
        on.index("tenant_id")
        < on.index("tender_id")
        < on.index("collection_id")
        < on.index("generation")
    )
    assert not any(w in sql.upper() for w in ("INSERT", "UPDATE", "DELETE"))


def test_lexical_table_name_must_be_a_bare_identifier() -> None:
    """The one interpolated value is validated."""
    for bad in ("chunks; DROP TABLE x", "a.b", "Chunks", ""):
        with pytest.raises(ValueError):
            PgLexicalSearch(None, bad, CircuitBreaker())  # type: ignore[arg-type]
    PgLexicalSearch(None, "document_chunks", CircuitBreaker())  # type: ignore[arg-type]


def test_a_point_missing_a_required_payload_field_is_dropped_not_guessed() -> None:
    """§4.10: document_role etc. are required; a point without them is skipped."""
    ok = {
        "chunk_id": "v1:1",
        "document_id": str(uuid4()),
        "document_version": 2,
        "text": "t",
        "document_title": "T",
        "document_role": "specification",
        "language": "en-GB",
    }
    cid = uuid4()
    cand = to_candidate(DenseHit("p", 0.5, ok), cid, Decimal("0.90"))
    assert cand is not None and cand.collection_id == cid and cand.weight == Decimal("0.90")
    assert cand.source_half == "dense" and cand.language == "en-GB"
    assert (
        to_candidate(
            DenseHit("p", 0.5, {k: v for k, v in ok.items() if k != "document_role"}),
            cid,
            Decimal(1),
        )
        is None
    )
    assert to_candidate(DenseHit("p", 0.5, {**ok, "document_id": "nope"}), cid, Decimal(1)) is None


# --- metrics -----------------------------------------------------------------------------------


def test_every_metric_passes_the_platform_naming_and_label_rules() -> None:
    """OBS-1/OBS-3 against fastapicommon's own checker (never tenant/user labels)."""
    labels = {
        "retrieval_stage_duration_seconds": ["stage"],
        "degradation_marker_total": ["stage", "reason"],
        "sufficiency_verdict_total": ["verdict", "section_type"],
        "required_input_gap_total": ["missing_role"],
        "rerank_skipped_total": ["reason"],
        "candidate_depth_band_total": ["profile_source", "band"],
        "retrieval_verdict_total": ["verdict"],
        "refiner_latency_seconds": ["refiner"],
    }
    registry = default_registry()
    for kind, names in METRIC_NAMES.items():
        for name in names:
            registry.check_custom(
                kind, P + name, labels.get(name, []), "llm", "retrieval-generation"
            )
    assert all(not any(x.endswith(("_id", "_email", "_ip")) for x in v) for v in labels.values())


def test_depth_bands_and_a_disabled_observer() -> None:
    """A distribution as a counter per band (OBS-3); metrics off means a no-op observer."""
    assert [depth_band(d) for d in (1, 8, 9, 24, 25, 48, 49)] == [
        "1-8",
        "1-8",
        "9-16",
        "17-24",
        "25-48",
        "25-48",
        "49+",
    ]
    off = build_observer(Settings(enable_metrics=False))
    off.marker("rerank", "deadline_exceeded")  # does not raise
