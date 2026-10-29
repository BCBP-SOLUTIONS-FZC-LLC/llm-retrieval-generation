"""Starts the SQS consumers (§12.1, §12.4) and stops them before the relays.

One consumer per queue and one DLQ forwarder per consumer (LD-75): a malformed or schema-violating
message reaches the DLQ and §16.4's alerts see it. The IAM queue decodes Glue-framed payloads
with eventcommon's decode-only codec. A queue whose URL is unset is not started, so local runs and
tests need no AWS. Lag, depth and DLQ counts are eventcommon's own ``platform_*`` metrics
(``queue_depth_interval``, ``platform_dlq_messages_total``), nothing hand-rolled.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from eventcommon.codec import GlueDecodeCodec
from eventcommon.consumer import DLQConfig, DLQPublisher, SQSConsumer, SQSDLQPublisher
from eventcommon.inbox import Store
from fastapicommon.logging import get_logger
from pgcommon import Pool

from rag.application.events import contracts as c
from rag.infrastructure.config import Settings
from rag.infrastructure.events import consumers as h
from rag.infrastructure.events.config import sqs_settings


@dataclass(frozen=True, slots=True)
class QueueSpec:
    """One consumer: its queue setting, ledger name, handler factory and codec."""

    queue: str
    url_setting: str
    consumer: str
    factory: Callable[[h.ConsumerServices, Store], h.Handler]
    glue: bool = False


log = get_logger(__name__)

QUEUES = (
    QueueSpec(
        "llm-document-rag-q", "document_queue_url", c.CONSUMER_PURGE_PROJECTION, h.purge_projection
    ),
    QueueSpec(
        "domainprofile-llm-invalidate-q",
        "domainprofile_queue_url",
        c.CONSUMER_PACK_INVALIDATION,
        h.pack_invalidation,
    ),
    QueueSpec(
        "membership-rag-q",
        "iam_membership_queue_url",
        c.CONSUMER_MEMBERSHIP,
        h.membership,
        glue=True,
    ),
    QueueSpec("tender-rag-q", "tender_queue_url", c.CONSUMER_TENDER_LIFECYCLE, h.tender_lifecycle),
    QueueSpec("usage-rag-q", "usage_queue_url", c.CONSUMER_QUOTA_RESUME, h.quota_resume),
)


@dataclass
class Consumers:
    """Running consumers and the DLQ forwarders they own."""

    consumers: dict[str, SQSConsumer] = field(default_factory=dict)
    forwarders: list[SQSDLQPublisher] = field(default_factory=list)
    tasks: list[asyncio.Task[None]] = field(default_factory=list)

    async def stop(self) -> None:
        """Stop polling and drain in-flight handlers, then close the forwarders."""
        await asyncio.gather(*(x.stop() for x in self.consumers.values()), return_exceptions=True)
        for task in self.tasks:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        for forwarder in self.forwarders:
            await forwarder.close()


def build_consumer(
    settings: Settings,
    spec: QueueSpec,
    services: h.ConsumerServices,
    pool: Pool,
    dlq: DLQPublisher,
    *,
    client: Any = None,
) -> SQSConsumer:
    """One ``SQSConsumer`` for ``spec`` (``client`` injects a fake SQS client in tests)."""
    handler = h.poison_safe(spec.factory(services, Store(pool, spec.consumer)), dlq)
    config, options = sqs_settings(settings, getattr(settings, spec.url_setting))
    return SQSConsumer(
        config,
        handler,
        dlq_publisher=dlq,
        codec=GlueDecodeCodec() if spec.glue else None,
        client=client,
        **options,
    )


def start_consumers(
    settings: Settings, services: h.ConsumerServices, generation_pool: Pool
) -> Consumers:
    """Start a consumer for every queue whose URL is configured."""
    running = Consumers()
    for spec in QUEUES:
        if not getattr(settings, spec.url_setting):
            continue
        dlq = SQSDLQPublisher(
            DLQConfig(
                region=settings.aws_region,
                endpoint_url=settings.aws_endpoint_url or None,
                consumer_name=spec.consumer,
                logger=log,
            )
        )
        consumer = build_consumer(settings, spec, services, generation_pool, dlq)
        running.forwarders.append(dlq)
        running.consumers[spec.queue] = consumer
        running.tasks.append(asyncio.create_task(consumer.start(), name=f"sqs-{spec.queue}"))
    return running
