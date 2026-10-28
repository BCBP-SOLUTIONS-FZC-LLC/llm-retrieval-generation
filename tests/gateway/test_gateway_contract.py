"""Our gateway client against the real gateway image and the model stub.

Run with ``make gateway-up`` then ``make test-gateway``; skipped when either is not running, and
excluded from ``make test`` and CI. What this proves: the formats, routes and error shapes our
client assumes are the ones Agent Router v1.2.0 accepts and passes through. What it does not
prove: anything about a real model, Bedrock, quotas, or latency (the backend is a test double).
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from uuid import uuid4

import httpx
import pytest

from rag.application.shared.gateway import (
    GatewayContext,
    GatewayQuotaRefused,
    GatewayResidencyRefused,
    GatewayUnavailable,
)
from rag.infrastructure.config import Settings
from rag.infrastructure.gateway.client import HttpModelGateway

pytestmark = pytest.mark.gateway

GATEWAY = os.environ.get("GATEWAY_TEST_URL", "http://localhost:1975")
BACKEND = os.environ.get("MODEL_STUB_URL", "http://localhost:8090")
TENANT = uuid4()
CTX = GatewayContext(plan="pro", residency_mode="standard", tenant_id=TENANT)


@pytest.fixture
async def gw() -> AsyncIterator[HttpModelGateway]:
    try:
        httpx.get(f"{BACKEND}/health", timeout=2).raise_for_status()
        httpx.post(
            f"{GATEWAY}/v1/embeddings", json={"model": "embed", "input": ["ping"]}, timeout=5
        ).raise_for_status()
    except httpx.HTTPError as exc:
        pytest.skip(f"gateway or model stub not running (make gateway-up): {exc}")
    gateway = HttpModelGateway(Settings(gateway_base_url=GATEWAY))
    yield gateway
    await gateway.aclose()


async def test_embed_goes_through_the_openai_route(gw: HttpModelGateway) -> None:
    vectors = await gw.embed(["hello world", "second"], ctx=CTX)
    assert len(vectors) == 2 and all(len(v) == 8 for v in vectors)


async def test_rerank_goes_through_the_cohere_v2_route_and_scores_follow_input_order(
    gw: HttpModelGateway,
) -> None:
    passages = ["the payment terms are net 30", "weather in Dubai", "terms of delivery"]
    scores = await gw.rerank("payment terms", passages, ctx=CTX, deadline_seconds=5)
    assert len(scores) == 3
    assert scores[0] > scores[2] > scores[1]


async def test_the_old_assumed_rerank_path_is_not_served() -> None:
    """``/v1/rerank`` was our assumption: the gateway answers 404; the path is /cohere/v2/rerank."""
    r = httpx.post(
        f"{GATEWAY}/v1/rerank",
        json={"model": "rerank", "query": "x", "documents": ["y"]},
        timeout=5,
    )
    assert r.status_code == 404


async def test_complete_returns_the_text_and_the_model(gw: HttpModelGateway) -> None:
    result = await gw.complete("judge", "sys", "is it supported", ctx=CTX, deadline_seconds=5)
    assert "fake" in result.text and result.model == "judge"


async def test_a_stream_delivers_deltas_then_usage(gw: HttpModelGateway) -> None:
    chunks = [
        c async for c in gw.generate_stream("draft", "sys", "write", ctx=CTX, deadline_seconds=10)
    ]
    assert "".join(c.delta for c in chunks).strip() == "fake answer from the stub"
    last = chunks[-1]
    assert last.completion_tokens == 5 and last.prompt_tokens > 0
    assert all(c.model == "draft" for c in chunks)


async def test_count_uses_the_tokenize_route_and_not_the_fallback_estimate(
    gw: HttpModelGateway,
) -> None:
    text = "one two three four five six seven"
    assert await gw.count([text], ctx=CTX) == [7]  # the fallback would say ceil(33 / 3) = 11


@pytest.mark.parametrize(
    ("marker", "error"),
    [("[[429]]", GatewayQuotaRefused), ("[[residency]]", GatewayResidencyRefused)],
)
async def test_refusals_pass_through_and_map_to_our_errors(
    gw: HttpModelGateway, marker: str, error: type[Exception]
) -> None:
    with pytest.raises(error):
        await gw.complete("draft", "sys", f"x {marker}", ctx=CTX, deadline_seconds=5)


async def test_a_quota_refusal_on_rerank_also_passes_through(gw: HttpModelGateway) -> None:
    with pytest.raises(GatewayQuotaRefused):
        await gw.rerank("x [[429]]", ["a"], ctx=CTX, deadline_seconds=5)


async def test_a_malformed_answer_is_an_outage_not_a_crash(gw: HttpModelGateway) -> None:
    with pytest.raises(GatewayUnavailable):
        await gw.complete("draft", "sys", "x [[malformed]]", ctx=CTX, deadline_seconds=5)


async def test_an_unconfigured_task_class_is_refused_as_unavailable(gw: HttpModelGateway) -> None:
    with pytest.raises(GatewayUnavailable):
        await gw.complete("not-a-task-class", "sys", "x", ctx=CTX, deadline_seconds=5)


async def test_our_headers_are_forwarded_to_the_backend_with_the_task_class_added(
    gw: HttpModelGateway,
) -> None:
    """``x-tenant-id``, ``x-residency-mode`` and ``x-plan`` reach the backend as sent, and the
    gateway adds ``x-ai-eg-model`` (the task class it routed on). The gateway itself can match a
    route on them too (checked by hand with a route requiring ``x-residency-mode: strict``)."""
    httpx.delete(f"{BACKEND}/__requests")
    await gw.embed(["header probe"], ctx=CTX)
    seen = [r for r in httpx.get(f"{BACKEND}/__requests").json() if r["path"] == "/v1/embeddings"]
    assert seen, "the backend saw no embeddings request"
    headers = seen[-1]["headers"]
    assert headers["x-ai-eg-model"] == "embed"
    assert headers["x-tenant-id"] == str(TENANT)
    assert headers["x-residency-mode"] == "standard" and headers["x-plan"] == "pro"
