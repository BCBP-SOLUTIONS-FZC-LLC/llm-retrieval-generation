"""Lifespan wiring with fake pools: container built, schema flag honoured, workers ordered."""

from __future__ import annotations

from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from rag.infrastructure.config import Settings
from rag.presentation import main
from rag.presentation.main import create_app, register_background_worker


class FakePool:
    """Records its close in the shared event log."""

    def __init__(self, name: str, log: list[str]) -> None:
        self.name, self.log = name, log

    async def drain_and_close(self) -> None:
        self.log.append(f"close {self.name}")


@pytest.fixture
def log(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Event log; pool factories and apply_schema are replaced by recorders."""
    events: list[str] = []

    async def corpus(_: Settings) -> Any:
        return FakePool("corpus", events)

    async def generation(_: Settings) -> Any:
        return FakePool("generation", events)

    async def schema(pool: Any) -> None:
        events.append(f"apply_schema {pool.name}")

    async def inbox_schema(pool: Any) -> None:
        events.append(f"apply_inbox_schema {pool.name}")

    monkeypatch.setattr(main, "create_corpus_pool", corpus)
    monkeypatch.setattr(main, "create_generation_pool", generation)
    monkeypatch.setattr(main, "apply_schema", schema)
    monkeypatch.setattr(main, "apply_inbox_schema", inbox_schema)
    return events


def _worker(log: list[str]) -> Any:
    async def start(app: FastAPI) -> Any:
        log.append("worker start")

        async def stop() -> None:
            log.append("worker stop")

        return stop

    return start


def test_lifespan_builds_container_and_stops_workers_before_pools(
    settings: Settings, log: list[str]
) -> None:
    """Workers run between pool open and close; the outbox schema is not applied by default."""
    app = create_app(settings)
    register_background_worker(app, _worker(log))
    with TestClient(app):
        assert app.state.container.generation_pool.name == "generation"
        assert app.state.container.settings is settings
    assert log == ["worker start", "worker stop", "close generation", "close corpus"]


def test_outbox_schema_applied_on_generation_when_flagged(
    settings: Settings, log: list[str]
) -> None:
    """outbox_apply_schema_on_startup applies eventcommon's schemas: the outbox on both databases,
    the inbox (the consumers' ledger) where the ledger lives."""
    app = create_app(settings.model_copy(update={"outbox_apply_schema_on_startup": True}))
    with TestClient(app):
        pass
    assert log[:3] == [
        "apply_schema generation",
        "apply_inbox_schema generation",
        "apply_schema corpus",
    ]


def _probe(app: FastAPI, path: str) -> int:
    with TestClient(app) as client:
        return client.get(path).status_code


def test_ready_gates_on_the_relays_only_when_events_are_enabled(log: list[str]) -> None:
    """Disabled events: nothing to wait for. Enabled: not ready until the relays report ready."""
    assert _probe(create_app(Settings(events_enabled=False)), "/ready") == 200

    app = create_app(Settings(events_enabled=True, generation_topic_arn="a", index_topic_arn="b"))
    state = {"ready": False}

    async def starter(application: FastAPI) -> Any:
        application.state.relay_ready = lambda: state["ready"]

        async def stop() -> None:
            return None

        return stop

    app.state.worker_starters = [starter]
    with TestClient(app) as client:
        assert client.get("/ready").status_code == 503
        assert client.get("/live").status_code == 200  # a stalled relay is not a restart
        assert client.get("/health").status_code == 200
        state["ready"] = True
        assert client.get("/ready").status_code == 200
        state["ready"] = False  # the relay stopped polling later
        assert client.get("/ready").status_code == 503


def test_ready_is_not_ready_when_the_relays_never_registered(log: list[str]) -> None:
    app = create_app(Settings(events_enabled=True, generation_topic_arn="a", index_topic_arn="b"))
    app.state.worker_starters = []  # no worker ever set relay_ready
    assert _probe(app, "/ready") == 503
