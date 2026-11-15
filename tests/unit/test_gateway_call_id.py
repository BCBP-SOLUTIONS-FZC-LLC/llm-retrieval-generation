"""Every Gateway call carries its own UUID v4 ``x-request-id``: the usage record's ``call_id``.

Commercial HLD v0.48 §10.2: the feed deduplicates on ``call_id``, so two calls of one request must
not share it, and the Gateway keeps only a v4 id. The platform HTTP client stamps the inbound
request's id on every outbound call; this service replaces it per call.
"""

from __future__ import annotations

from uuid import UUID, uuid4

import httpx
from fastapicommon.context import RequestContext, request_context_scope

from rag.application.shared.gateway import GatewayContext
from rag.infrastructure.config import Settings
from rag.infrastructure.gateway import client as gateway_client
from rag.infrastructure.gateway.client import HttpModelGateway

CTX = GatewayContext(plan="pro", tenant_id=uuid4())


def _real_client_over(handler: httpx.MockTransport, monkeypatch: object) -> HttpModelGateway:
    real = gateway_client.create_platform_http_client

    def build(**kwargs: object) -> httpx.AsyncClient:
        return real(transport=handler, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(gateway_client, "create_platform_http_client", build)  # type: ignore[attr-defined]
    return HttpModelGateway(Settings(gateway_base_url="http://gw.test"))


async def test_each_call_has_a_distinct_uuid_v4_and_keeps_the_correlation_id(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"data": [{"embedding": [1.0]}]})

    gw = _real_client_over(httpx.MockTransport(handler), monkeypatch)
    inbound = RequestContext(request_id="NOT-A-UUID-FROM-THE-CALLER", correlation_id="corr-1")
    with request_context_scope(inbound):
        for _ in range(3):
            await gw.embed(["t"], ctx=CTX)
    await gw.aclose()

    ids = [r.headers["x-request-id"] for r in seen]
    assert len(set(ids)) == 3 and "NOT-A-UUID-FROM-THE-CALLER" not in ids
    assert all(UUID(i).version == 4 for i in ids)
    assert {r.headers["x-correlation-id"] for r in seen} == {"corr-1"}


async def test_a_call_with_no_request_context_still_gets_an_id(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"data": [{"embedding": [1.0]}]})

    gw = _real_client_over(httpx.MockTransport(handler), monkeypatch)
    await gw.embed(["t"], ctx=CTX)  # a sweep or consumer: no request context is bound
    await gw.aclose()
    assert UUID(seen[0].headers["x-request-id"]).version == 4
