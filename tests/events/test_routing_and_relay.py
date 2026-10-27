"""RoutingPublisher (§18.3, LD-11) and the one-relay-per-table wiring (OBX-3)."""

from __future__ import annotations

import logging
from typing import Any

import pytest
from eventcommon import new_envelope
from eventcommon.errors import BatchError, BatchFailure, RetryableError
from eventcommon.mock import MockPublisher

from rag.application.events import contracts as c
from rag.infrastructure.config import Settings
from rag.infrastructure.events import relay
from rag.infrastructure.events.routing import RoutingPublisher


def env(type_: str) -> Any:
    return new_envelope(type_, c.SOURCE, {"k": 1}, tenant_id="0a3f0000-0000-7000-8000-000000000001")


def router() -> tuple[RoutingPublisher, MockPublisher, MockPublisher]:
    gen, idx = MockPublisher(), MockPublisher()
    return RoutingPublisher(gen, idx), gen, idx


@pytest.mark.parametrize("type_", sorted(c.INDEX_EVENT_TYPES))
async def test_index_types_route_to_the_index_topic_by_name(type_: str) -> None:
    """§18.3: the seven named types, ``ProfileCacheRefreshed`` included."""
    r, gen, idx = router()
    await r.publish(env(type_))
    assert [e.type for e in idx.published] == [type_] and gen.published == []


@pytest.mark.parametrize("type_", sorted(c.GENERATION_EVENT_TYPES))
async def test_generation_types_route_to_the_generation_topic(type_: str) -> None:
    """Everything else, including the two that do not lead with ``Generation``."""
    r, gen, idx = router()
    await r.publish(env(type_))
    assert [e.type for e in gen.published] == [type_] and idx.published == []
    assert r.unrouted == 0


async def test_an_unknown_type_defaults_to_generation_with_a_warning_and_never_raises(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A KeyError would dead-letter and delete the row; a wrong topic is recoverable."""
    r, gen, idx = router()
    with caplog.at_level(logging.WARNING):
        await r.publish(env("BrandNewThing"))
        await r.publish_batch([env("AnotherNewThing")])
    assert len(gen.published) == 2 and idx.published == [] and r.unrouted == 2
    assert "not registered" in caplog.text


async def test_a_mixed_batch_is_split_by_topic() -> None:
    """One claim loop, two topics, nothing scattered."""
    r, gen, idx = router()
    batch = [env(c.SECTION_COMPLETED), env(c.INDEX_GENERATION_ACTIVATED), env(c.GENERATION_STARTED)]
    await r.publish_batch(batch)
    assert [e.id for e in gen.published] == [batch[0].id, batch[2].id]
    assert [e.id for e in idx.published] == [batch[1].id]


async def test_batch_failures_are_reported_by_envelope_id_without_stopping_the_other_topic() -> (
    None
):
    """A failure on one topic marks only its rows; the other group still publishes."""
    r, gen, idx = router()
    a, b, c_ = env(c.SECTION_COMPLETED), env(c.INDEX_GENERATION_ACTIVATED), env(c.OUTLINE_READY)
    gen.set_batch_error(BatchError([BatchFailure(c_.id, "Throttled", "slow", True)]))
    with pytest.raises(BatchError) as caught:
        await r.publish_batch([a, b, c_])
    assert [f.id for f in caught.value.failures] == [c_.id]
    assert [e.id for e in idx.published] == [b.id]  # not blocked
    assert [e.id for e in gen.published] == [a.id]


async def test_a_group_error_becomes_per_row_failures_with_the_retryable_flag() -> None:
    """Any other exception fails its whole group, transient only when the cause is."""
    r, gen, idx = router()
    idx.set_error(RetryableError(RuntimeError("503")))
    batch = [env(c.INDEX_GENERATION_ACTIVATED), env(c.COLLECTION_PROVISIONED)]
    with pytest.raises(BatchError) as caught:
        await r.publish_batch(batch)
    assert {f.id for f in caught.value.failures} == {e.id for e in batch}
    assert all(f.retryable for f in caught.value.failures)
    idx.set_error(ValueError("bad"))
    with pytest.raises(BatchError) as caught:
        await r.publish_batch(batch[:1])
    assert not caught.value.failures[0].retryable


class FakeRunner:
    """Stands in for ``eventcommon.outbox.Runner``."""

    instances: list[FakeRunner] = []

    def __init__(self, config: Any) -> None:
        self.config = config
        self.log: list[str] = []
        self.ready = __import__("asyncio").Event()
        FakeRunner.instances.append(self)

    async def start(self) -> None:
        self.log.append("start")

    async def stop(self) -> str | None:
        self.log.append("stop")
        return (
            "drain timeout"
            if len(FakeRunner.instances) > 1 and self is FakeRunner.instances[0]
            else None
        )


class FakeSNS:
    def __init__(self) -> None:
        self.closed = False

    async def close(self) -> None:
        self.closed = True


async def test_one_relay_per_database_sharing_one_router(monkeypatch: pytest.MonkeyPatch) -> None:
    """The recorded decision: one claim loop per table, one routing publisher for both."""
    FakeRunner.instances = []
    monkeypatch.setattr(relay, "Runner", FakeRunner)
    r, _, _ = router()
    sns = FakeSNS()
    relays = relay.start_relays(
        Settings(),
        {"generation": object(), "corpus": object()},
        router=r,
        publishers=[sns],  # type: ignore[list-item,dict-item]
    )
    assert set(relays.runners) == {"generation", "corpus"}
    assert {id(x.config.publisher) for x in FakeRunner.instances} == {id(r)}
    assert relays.ready() is False
    for x in FakeRunner.instances:
        x.ready.set()
    assert relays.ready() is True
    await relays.stop()
    assert all("stop" in x.log for x in FakeRunner.instances) and sns.closed


def test_runner_config_follows_settings_and_lease_is_valid() -> None:
    """The lease must cover ``batch_size x publish_timeout`` or the Runner refuses to start."""
    from eventcommon.outbox import Runner

    cfg = relay.runner_config(Settings(), None, MockPublisher())  # type: ignore[arg-type]
    cfg.store = object()  # type: ignore[assignment]  # no pool needed to check the lease rule
    Runner(cfg)
    assert (cfg.batch_size, cfg.poll_interval) == (50, 5.0)
