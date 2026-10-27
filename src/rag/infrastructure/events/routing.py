"""The routing ``Publisher`` of the relay (§18.3, LD-11, OBX-3).

A ``SNSPublisher`` holds one topic ARN and the relay's claim query has no ``event_type``
predicate, so two relays over one table would scatter both topics. This service therefore runs
one claim loop per table, all publishing through *this* object, which picks the ARN from the
event type by name (PascalCase types match no prefix) and delegates to an ``SNSPublisher`` per
ARN. It implements eventcommon's ``Publisher`` protocol and nothing else.

It **never raises on an unrecognised type**: the relay treats any error that is not declared
retryable as non-retryable and dead-letters the row, deleting it from ``outbox_events``. An event
on the wrong topic can be recovered; one in a dead-letter table nobody alerts on cannot. So an
unknown type takes the default branch (``llm-generation-events``) with a warning. The LLD's
warning *metric* is Tier 3 (``llm_retrieval_generation_*``), and the ``llm`` domain is not
registered in the platform registry yet (OQ-103), so for now it is a counter attribute plus a log
line.
"""

from __future__ import annotations

import logging
from typing import Any

from eventcommon.envelope import Envelope
from eventcommon.errors import BatchError, BatchFailure, is_retryable
from eventcommon.publisher.sns import Publisher

from rag.application.events.contracts import GENERATION_EVENT_TYPES, INDEX_EVENT_TYPES

log = logging.getLogger(__name__)


class RoutingPublisher:
    """Routes each envelope to the publisher of its topic, by event type name."""

    def __init__(self, generation: Publisher, index: Publisher) -> None:
        self._generation, self._index = generation, index
        self.unrouted = 0
        """Events of an unregistered type sent to the default topic (the warning counter)."""

    def route(self, event_type: str) -> Publisher:
        """The publisher for ``event_type``; unknown types default to the generation topic."""
        if event_type in INDEX_EVENT_TYPES:
            return self._index
        if event_type not in GENERATION_EVENT_TYPES:
            self.unrouted += 1
            log.warning(
                "event type %r is not registered; routed to llm-generation-events", event_type
            )
        return self._generation

    async def publish(self, env: Envelope[Any]) -> None:
        """Publish one envelope to its topic."""
        await self.route(env.type).publish(env)

    async def publish_batch(self, envs: list[Envelope[Any]]) -> None:
        """Split by topic, publish each group, and report every failure by envelope id.

        A ``BatchError`` from one group does not stop the others; the failures are merged into one
        ``BatchError`` so the relay marks only the failed rows. Any other error fails its whole
        group, transient when the cause is retryable.
        """
        groups: dict[int, tuple[Publisher, list[Envelope[Any]]]] = {}
        for env in envs:
            target = self.route(env.type)
            groups.setdefault(id(target), (target, []))[1].append(env)
        failures: list[BatchFailure] = []
        for target, members in groups.values():
            try:
                await target.publish_batch(members)
            except BatchError as exc:
                failures.extend(exc.failures)
            except Exception as exc:  # noqa: BLE001 - mapped to per-row failures, never dropped
                retryable = is_retryable(exc)
                failures.extend(
                    BatchFailure(e.id, "PublishError", str(exc) or type(exc).__name__, retryable)
                    for e in members
                )
        if failures:
            raise BatchError(failures)
