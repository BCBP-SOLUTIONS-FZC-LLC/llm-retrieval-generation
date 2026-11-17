"""Corpus transaction error mapping and the consumer runtime's start/stop."""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from pgcommon import AcquireTimeoutError

from rag.domain.shared.errors import DomainError, ErrorCode
from rag.infrastructure.config import Settings
from rag.infrastructure.events import runtime
from rag.infrastructure.persistence.corpus.transactions import CorpusTransactionRunner
from rag.infrastructure.persistence.transactions import PgTransactionRunner


async def _run(monkeypatch: pytest.MonkeyPatch, error: Exception | None) -> Any:
    async def fake(self: Any, fn: Any, *, read_only: bool = False) -> str:
        if error:
            raise error
        return "ok"

    monkeypatch.setattr(PgTransactionRunner, "run_in_tx", fake)
    return await CorpusTransactionRunner(MagicMock()).run_in_tx(AsyncMock())


async def test_corpus_tx_passes_results_and_domain_errors_through(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Success and ``DomainError`` are untouched."""
    assert await _run(monkeypatch, None) == "ok"
    with pytest.raises(DomainError) as exc:
        await _run(monkeypatch, DomainError(ErrorCode.COLLECTION_NOT_FOUND, "x"))
    assert exc.value.code == ErrorCode.COLLECTION_NOT_FOUND


@pytest.mark.parametrize("error", [OSError("refused"), AcquireTimeoutError("slow")])
async def test_corpus_tx_maps_an_unreachable_store_to_503(
    monkeypatch: pytest.MonkeyPatch, error: Exception
) -> None:
    """Connection-class failures are SCOPE_STORE_UNAVAILABLE (CG-4: no default is given)."""
    with pytest.raises(DomainError) as exc:
        await _run(monkeypatch, error)
    assert exc.value.code == ErrorCode.SCOPE_STORE_UNAVAILABLE


async def test_corpus_tx_reraises_everything_else(monkeypatch: pytest.MonkeyPatch) -> None:
    """A bug is not an outage."""
    with pytest.raises(ValueError):
        await _run(monkeypatch, ValueError("bug"))


async def test_consumers_stop_drains_then_closes_forwarders() -> None:
    """Consumers stop (errors tolerated), tasks finish, forwarders close."""
    forwarder = MagicMock(close=AsyncMock())
    bad = MagicMock(stop=AsyncMock(side_effect=RuntimeError("x")))
    good = MagicMock(stop=AsyncMock())

    async def boom() -> None:
        raise RuntimeError("task failed")

    running = runtime.Consumers(
        consumers={"a": bad, "b": good},
        forwarders=[forwarder],
        tasks=[asyncio.create_task(boom())],
    )
    await running.stop()
    good.stop.assert_awaited_once()
    forwarder.close.assert_awaited_once()


async def test_start_consumers_starts_one_per_configured_queue(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Only the queue with a URL gets a consumer, a DLQ forwarder and a polling task."""
    started = asyncio.Event()

    async def start() -> None:
        started.set()

    consumer = MagicMock(start=start)
    monkeypatch.setattr(runtime, "SQSDLQPublisher", lambda cfg: MagicMock(config=cfg))
    monkeypatch.setattr(runtime, "build_consumer", lambda *a, **k: consumer)
    settings = Settings(tender_queue_url="http://q/tender-rag-q")
    running = runtime.start_consumers(settings, MagicMock(), MagicMock())
    assert list(running.consumers) == ["tender-rag-q"]
    assert len(running.forwarders) == len(running.tasks) == 1
    await asyncio.wait_for(started.wait(), 1)
    await asyncio.gather(*running.tasks)


async def test_generation_runner_maps_an_unreachable_database_to_its_503_code(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """R-5 and the other generation reads answered 500 'unclassified error' when the pool failed."""
    from rag.domain.shared.errors import DomainError, ErrorCode
    from rag.infrastructure.persistence import transactions as t

    async def refused(pool: Any, fn: Any, *, read_only: bool = False) -> Any:
        raise ConnectionRefusedError("connect call failed")

    monkeypatch.setattr(t, "run_in_tx", refused)
    unwired = t.PgTransactionRunner(MagicMock())
    with pytest.raises(ConnectionRefusedError):  # no code asked for: the original error stays
        await unwired.run_in_tx(AsyncMock())
    runner = t.PgTransactionRunner(MagicMock(), unavailable=ErrorCode.ARTEFACT_STORE_UNWRITABLE)
    with pytest.raises(ConnectionRefusedError):  # a write keeps the original error
        await runner.run_in_tx(AsyncMock())
    with pytest.raises(DomainError) as e:  # a read is a specific 503
        await runner.run_in_tx(AsyncMock(), read_only=True)
    assert e.value.code is ErrorCode.ARTEFACT_STORE_UNWRITABLE

    async def bug(pool: Any, fn: Any, *, read_only: bool = False) -> Any:
        raise KeyError("a bug in a query is not an outage")

    monkeypatch.setattr(t, "run_in_tx", bug)
    with pytest.raises(KeyError):
        await runner.run_in_tx(AsyncMock(), read_only=True)

    async def domain(pool: Any, fn: Any, *, read_only: bool = False) -> Any:
        raise DomainError(ErrorCode.RETRIEVAL_SET_NOT_FOUND, "x")

    monkeypatch.setattr(t, "run_in_tx", domain)
    with pytest.raises(DomainError) as e2:  # a domain answer passes through untouched
        await runner.run_in_tx(AsyncMock(), read_only=True)
    assert e2.value.code is ErrorCode.RETRIEVAL_SET_NOT_FOUND
