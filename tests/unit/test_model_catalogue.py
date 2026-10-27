"""C-M1's Gateway adapter: parsing, and every failure becomes 503 INFERENCE_UNAVAILABLE."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest

from rag.domain.shared.errors import DomainError, ErrorCode
from rag.infrastructure.config import Settings
from rag.infrastructure.gateway import model_catalogue
from rag.infrastructure.gateway.model_catalogue import HttpModelCatalogue, parse_catalogue

BODY = {
    "models": [
        {
            "id": "gen-large-eu",
            "residency": "eu",
            "task_classes": ["draft", "judge"],
            "plans": ["pro"],
        }
    ],
    "as_of": "2026-04-02T09:00:00Z",
}


def _serve(
    monkeypatch: pytest.MonkeyPatch, handler: Callable[[httpx.Request], httpx.Response]
) -> None:
    def client(**kwargs: Any) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(handler), **kwargs)

    monkeypatch.setattr(model_catalogue, "create_platform_http_client", client)


def test_parse_catalogue_shape() -> None:
    """The LLD shape: id, residency, task_classes, plans, and the Gateway's as_of."""
    dto = parse_catalogue(BODY, datetime(2030, 1, 1, tzinfo=UTC))
    [m] = dto.models
    assert (m.id, m.residency, m.task_classes, m.plans) == (
        "gen-large-eu",
        "eu",
        ["draft", "judge"],
        ["pro"],
    )
    assert dto.as_of == datetime(2026, 4, 2, 9, 0, tzinfo=UTC)
    assert parse_catalogue({"models": []}, datetime(2030, 1, 1, tzinfo=UTC)).as_of.year == 2030


async def test_list_models_reads_the_gateway(
    monkeypatch: pytest.MonkeyPatch, settings: Settings
) -> None:
    """GET {gateway_base_url}/v1/models, and the answer is passed through."""
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return httpx.Response(200, json=BODY)

    _serve(monkeypatch, handler)
    dto = await HttpModelCatalogue(settings).list_models()
    assert (
        seen == ["http://model-gateway.internal/v1/models"] and dto.models[0].id == "gen-large-eu"
    )


@pytest.mark.parametrize(
    "handler",
    [
        lambda r: httpx.Response(500),
        lambda r: httpx.Response(200, text="not json"),
        lambda r: httpx.Response(200, json={"nope": []}),
        lambda r: httpx.Response(200, json=[1, 2]),
    ],
)
async def test_unreadable_catalogue_is_inference_unavailable(
    monkeypatch: pytest.MonkeyPatch, settings: Settings, handler: Callable[..., httpx.Response]
) -> None:
    """§5.7.18: where the catalogue cannot be read, 503 INFERENCE_UNAVAILABLE."""
    _serve(monkeypatch, handler)
    with pytest.raises(DomainError) as err:
        await HttpModelCatalogue(settings).list_models()
    assert err.value.code is ErrorCode.INFERENCE_UNAVAILABLE


async def test_connection_failure_is_inference_unavailable(
    monkeypatch: pytest.MonkeyPatch, settings: Settings
) -> None:
    """A refused connection is the same 503."""

    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    _serve(monkeypatch, refuse)
    with pytest.raises(DomainError) as err:
        await HttpModelCatalogue(settings).list_models()
    assert err.value.code is ErrorCode.INFERENCE_UNAVAILABLE
