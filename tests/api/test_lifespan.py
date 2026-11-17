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


DOWN: set[str] = set()
"""Pool names the fake writability probe refuses (the pools here are fakes with a ``name``)."""


class _FakeProbe:
    def __init__(self, tx: Any, timeout_seconds: float = 0.1) -> None:
        self._name = tx._pool.name

    async def require_writable(self) -> None:
        if self._name in DOWN:
            raise ConnectionError("down")


@pytest.fixture(autouse=True)
def _databases_up(monkeypatch: pytest.MonkeyPatch) -> None:
    """Readiness proves a writable primary on each database; the fakes here answer for it."""
    DOWN.clear()
    monkeypatch.setattr(main, "PgStoreProbe", _FakeProbe)


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


@pytest.mark.parametrize("down", ["corpus", "generation"])
def test_ready_needs_both_databases_to_take_a_write_transaction(
    log: list[str], down: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """LLD §18: llm_generation writable and llm_corpus primary reachable. /ready answered 200 with
    pgbouncer stopped; /live and /health stay up so the pod is not restarted for it."""
    monkeypatch.setattr(main, "READINESS_CACHE_SECONDS", 0.0)  # every poll probes
    app = create_app(Settings(events_enabled=False))
    with TestClient(app) as client:
        assert client.get("/ready").status_code == 200
        DOWN.add(down)
        assert client.get("/ready").status_code == 503
        assert client.get("/live").status_code == 200
        assert client.get("/health").status_code == 200
        DOWN.clear()
        assert client.get("/ready").status_code == 200


def test_ready_is_not_ready_before_the_container_exists() -> None:
    """Before the lifespan has built the container nothing proves a database is writable."""
    app = create_app(Settings(events_enabled=False))
    check = main._databases_ready(app)
    import asyncio

    assert asyncio.run(check()) is False


def test_ready_polls_inside_a_second_share_one_probe(
    log: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """/ready needs no credentials, so a flood of polls must not open a transaction each."""
    calls: list[str] = []

    class Counting(_FakeProbe):
        async def require_writable(self) -> None:
            calls.append(self._name)

    monkeypatch.setattr(main, "PgStoreProbe", Counting)
    app = create_app(Settings(events_enabled=False))
    with TestClient(app) as client:
        for _ in range(20):
            assert client.get("/ready").status_code == 200
    assert sorted(calls) == ["corpus", "generation"]  # two probes, once, for twenty polls


def test_ready_ignores_the_callers_identity_headers(
    log: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """/ready needs no credentials: an x-user-id with no tenant made the GUC provider refuse, which
    read as 'not ready' for every caller for the cache window. The probe runs in a clean context."""
    from fastapicommon.context import get_request_context_or_none

    seen: list[object] = []

    class Spy(_FakeProbe):
        async def require_writable(self) -> None:
            seen.append(get_request_context_or_none())  # None: no request context in the probe

    monkeypatch.setattr(main, "PgStoreProbe", Spy)
    monkeypatch.setattr(main, "READINESS_CACHE_SECONDS", 0.0)
    app = create_app(Settings(events_enabled=False))
    with TestClient(app) as client:
        headers = {"x-user-id": "aaaaaaaa-0000-4000-8000-000000000001"}
        assert client.get("/ready", headers=headers).status_code == 200
    assert seen == [None, None]  # the probe never saw the caller's request or its headers


async def test_concurrent_ready_polls_share_one_probe(
    log: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Single flight: callers that arrive while a probe runs take its answer."""
    import asyncio

    import httpx

    calls: list[str] = []

    class Slow(_FakeProbe):
        async def require_writable(self) -> None:
            calls.append(self._name)
            await asyncio.sleep(0.05)

    monkeypatch.setattr(main, "PgStoreProbe", Slow)
    app = create_app(Settings(events_enabled=False))
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
            replies = await asyncio.gather(*(client.get("/ready") for _ in range(25)))
    assert {r.status_code for r in replies} == {200}
    assert sorted(calls) == ["corpus", "generation"]
