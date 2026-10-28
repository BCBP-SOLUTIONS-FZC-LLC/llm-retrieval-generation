"""The outbox relay, in process (EVT-8), one claim loop per database over a shared router.

**Decision (the events slice's open question).** Corpus events are enqueued on the connection of
the ``llm_corpus`` transaction that changes the state, so ``outbox_events`` exists in both
databases. Moving the corpus enqueue into a second transaction on ``llm_generation`` would break
EVT-8 (a crash between the two commits loses or invents an event), so it is rejected. This module
runs **one relay per table**, both publishing through the same ``RoutingPublisher``. OBX-3's
reasoning concerns two relays over *one* table, each holding its own ARN, claiming whatever is
pending and scattering the topics; here each loop claims from its own table and the routing is by
event type, so nothing can land on the wrong topic. Replicas run the same configuration and
``FOR UPDATE SKIP LOCKED`` splits rows between them.

Order of shutdown: consumers stop first (they enqueue nothing but may be mid-effect), then the
relays drain their in-flight publish, then the SNS clients close, and the lifespan closes the
pools last.
"""

from __future__ import annotations

import asyncio
import contextlib
from dataclasses import dataclass, field

from eventcommon.outbox import Runner
from eventcommon.publisher import SNSPublisher
from fastapicommon.logging import get_logger
from pgcommon import Pool

from rag.infrastructure.config import Settings
from rag.infrastructure.events.config import runner_config, sns_config
from rag.infrastructure.events.routing import RoutingPublisher

log = get_logger(__name__)


@dataclass
class Relays:
    """The running relays: one ``Runner`` per database and the SNS clients behind the router."""

    runners: dict[str, Runner]
    publishers: list[SNSPublisher]
    tasks: list[asyncio.Task[None]] = field(default_factory=list)

    def ready(self) -> bool:
        """Every relay has completed its first successful poll (feeds readiness, §18.1)."""
        return all(r.ready.is_set() for r in self.runners.values())

    async def stop(self) -> None:
        """Drain in-flight publishes, wait for the loops, close the SNS clients."""
        for name, runner in self.runners.items():
            warning = await runner.stop()
            if warning:  # a drain timeout is visible, never silent (§18)
                log.warning("outbox_relay_warning", relay=name, warning=str(warning))
        for task in self.tasks:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        for publisher in self.publishers:
            await publisher.close()


def _sns(settings: Settings, topic_arn: str) -> SNSPublisher:
    return SNSPublisher(sns_config(settings, topic_arn))


def start_relays(
    settings: Settings,
    pools: dict[str, Pool],
    *,
    router: RoutingPublisher | None = None,
    publishers: list[SNSPublisher] | None = None,
) -> Relays:
    """Start one relay per entry of ``pools`` (``{"corpus": ..., "generation": ...}``).

    ``router`` / ``publishers`` replace the SNS-backed defaults (tests use eventcommon's mock).
    """
    owned = publishers if publishers is not None else []
    if router is None:
        generation = _sns(settings, settings.generation_topic_arn)
        index = _sns(settings, settings.index_topic_arn)
        owned = [generation, index]
        router = RoutingPublisher(generation, index)
    runners = {name: Runner(runner_config(pool, router)) for name, pool in pools.items()}
    relays = Relays(runners, owned)
    relays.tasks = [
        asyncio.create_task(r.start(), name=f"outbox-relay-{name}") for name, r in runners.items()
    ]
    return relays
