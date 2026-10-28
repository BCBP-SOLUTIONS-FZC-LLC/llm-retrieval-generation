"""The gateway's wire details (paths, header names) are settings, not code.

The service stays agnostic of any one gateway: ``infrastructure/gateway/client.py`` is the only
place that knows them, and a different gateway is a configuration change. The defaults are Agent
Router's; custom values must reach the wire on every call.
"""

from __future__ import annotations

import json
from typing import Any
from uuid import uuid4

import httpx
import pytest
from pydantic import ValidationError

from rag.application.shared.gateway import GatewayContext
from rag.infrastructure.config import Settings
from rag.infrastructure.gateway import model_catalogue
from rag.infrastructure.gateway.client import HttpModelGateway
from rag.infrastructure.gateway.model_catalogue import HttpModelCatalogue

TENANT = uuid4()
CTX = GatewayContext(plan="pro", residency_mode="strict", preferred_model="m1", tenant_id=TENANT)

CUSTOM = {
    "gateway_chat_path": "/chat",
    "gateway_embeddings_path": "/embed",
    "gateway_rerank_path": "/v1/rerank",
    "gateway_tokenize_path": "/tok",
    "gateway_models_path": "/models",
    "gateway_tenant_header": "X-Org",
    "gateway_residency_header": "x-region-mode",
    "gateway_plan_header": "x-tier",
    "gateway_preferred_model_header": "x-model-hint",
}


def _gateway(settings: Settings, seen: list[httpx.Request]) -> HttpModelGateway:
    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if json.loads(request.content or b"{}").get("stream"):
            frame = b'data: {"choices": [{"delta": {"content": "x"}}], "model": "m"}\n\n'
            return httpx.Response(200, content=frame + b"data: [DONE]\n\n")
        return httpx.Response(
            200,
            json={
                "data": [{"embedding": [1.0]}],
                "results": [{"index": 0, "relevance_score": 0.5}],
                "choices": [{"message": {"content": "{}"}}],
                "count": 3,
            },
        )

    g = HttpModelGateway(settings)
    g._client = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://gw.test")
    return g


async def _exercise(g: HttpModelGateway) -> None:
    await g.embed(["t"], ctx=CTX)
    await g.rerank("q", ["a"], ctx=CTX, deadline_seconds=1)
    await g.complete("judge", "s", "u", ctx=CTX, deadline_seconds=1)
    await g.count(["one two"], ctx=CTX)
    chunks = [c async for c in g.generate_stream("draft", "s", "u", ctx=CTX, deadline_seconds=2)]
    assert [c.delta for c in chunks] == ["x"]  # the stream really ran, on the configured path


async def test_the_defaults_are_agent_routers_paths_and_the_usual_headers() -> None:
    seen: list[httpx.Request] = []
    await _exercise(_gateway(Settings(), seen))
    assert [r.url.path for r in seen] == [
        "/v1/embeddings",
        "/cohere/v2/rerank",
        "/v1/chat/completions",
        "/tokenize",
        "/v1/chat/completions",
    ]
    h = seen[0].headers
    assert h["x-tenant-id"] == str(TENANT) and h["x-residency-mode"] == "strict"
    assert h["x-plan"] == "pro" and h["x-preferred-model"] == "m1"


async def test_custom_paths_and_header_names_reach_the_wire_on_every_call() -> None:
    seen: list[httpx.Request] = []
    await _exercise(_gateway(Settings(**CUSTOM), seen))
    assert [r.url.path for r in seen] == ["/embed", "/v1/rerank", "/chat", "/tok", "/chat"]
    for r in seen:
        assert r.headers["x-org"] == str(TENANT)  # header names are case-insensitive on the wire
        assert r.headers["x-region-mode"] == "strict"
        assert r.headers["x-tier"] == "pro" and r.headers["x-model-hint"] == "m1"
        assert "x-tenant-id" not in r.headers and "x-plan" not in r.headers


async def test_the_model_catalogue_path_is_configurable(monkeypatch: pytest.MonkeyPatch) -> None:
    paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        return httpx.Response(200, json={"models": [], "as_of": "2026-04-02T09:00:00Z"})

    def client(**kwargs: Any) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(handler), **kwargs)

    monkeypatch.setattr(model_catalogue, "create_platform_http_client", client)
    await HttpModelCatalogue(Settings(gateway_models_path="/models")).list_models()
    await HttpModelCatalogue(Settings()).list_models()
    assert paths == ["/models", "/v1/models"]


@pytest.mark.parametrize(
    "overrides",
    [
        {"gateway_rerank_path": "v1/rerank"},  # not absolute
        {"gateway_chat_path": "/a b"},  # a space
        {"gateway_embeddings_path": ""},
        {"gateway_tenant_header": "x tenant"},
        {"gateway_plan_header": "x-plan:"},
        {"gateway_residency_header": ""},
    ],
)
def test_a_bad_path_or_header_name_stops_startup(overrides: dict[str, str]) -> None:
    with pytest.raises(ValidationError):
        Settings(**overrides)


def test_header_names_are_normalised_to_lower_case() -> None:
    assert Settings(gateway_tenant_header="X-Org").gateway_tenant_header == "x-org"
