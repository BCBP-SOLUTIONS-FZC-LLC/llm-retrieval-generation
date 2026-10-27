"""``EventPublisher`` over the eventcommon outbox: the event row joins the caller's transaction."""

from __future__ import annotations

from eventcommon import new_envelope
from eventcommon.outbox import enqueue
from fastapicommon.context import get_request_context_or_none

from rag.application.events.contracts import SOURCE
from rag.application.shared.events import DomainEvent
from rag.application.shared.ports import TxHandle
from rag.infrastructure.events.schemas import validate_published

EVENT_SOURCE = SOURCE
"""Envelope ``source`` of every event this service publishes: ``llm-rag-service`` (§12.2)."""


class OutboxEventPublisher:
    """Enqueues each event in ``outbox_events`` on the connection of the state change (EVT-8).

    ``tx`` is the connection ``run_in_tx`` handed the use case, so ``eventcommon`` can verify
    it is inside a transaction. The outbox table must exist in the database that connection
    belongs to; the relay that publishes it is ``infrastructure.events.relay``. The payload is
    checked against its schema first (fail closed, EVT-9), and eventcommon rejects an envelope
    over 240 KB (OBX-2).
    """

    async def publish(self, tx: TxHandle, event: DomainEvent) -> None:
        """Wrap ``event`` in the canonical envelope and enqueue it."""
        validate_published(event.type, event.payload)
        context = get_request_context_or_none()
        envelope = new_envelope(
            event.type,
            EVENT_SOURCE,
            event.payload,
            tenant_id=str(event.tenant_id),
            subject=event.subject,
            trace_id=(context.trace_id or "") if context else "",
            correlation_id=event.correlation_id or (context.correlation_id if context else ""),
        )
        await enqueue(tx, envelope)
