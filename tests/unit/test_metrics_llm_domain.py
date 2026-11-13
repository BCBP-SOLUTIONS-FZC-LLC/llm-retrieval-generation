"""The platform metrics registry accepts the ``llm`` domain (fastapicommon 0.6.0, LLD OQ-103)."""

from __future__ import annotations

from collections.abc import Iterator
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from fastapicommon.metrics import platform as platform_metrics
from prometheus_client import REGISTRY

from rag.infrastructure.config import Settings
from rag.presentation.main import create_app


@pytest.fixture
def fresh_metrics(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """The library's metrics state is process-wide: give this test its own and undo it after."""
    state = platform_metrics._State()  # noqa: SLF001 - no public reset exists
    monkeypatch.setattr(platform_metrics, "_state", state)
    yield
    if state.platform is not None:
        REGISTRY.unregister(state.platform.collector)


def test_app_serves_metrics_under_the_llm_domain_with_the_route_as_the_label(
    fresh_metrics: None,
) -> None:
    """Metrics on with ``OBSERVABILITY_DOMAIN=llm`` starts; the request label is the route."""
    settings = Settings(
        environment="test", enable_tracing=False, enable_metrics=True, observability_domain="llm"
    )
    client = TestClient(create_app(settings))  # no lifespan: no pools or relays are needed
    collection_id = str(uuid4())
    client.get(
        f"/api/v1/internal/collections/{collection_id}", headers={"x-tenant-id": str(uuid4())}
    )
    body = client.get("/metrics").text
    assert 'domain="llm"' in body
    assert 'route="/api/v1/internal/collections/:collection_id"' in body
    assert collection_id not in body
