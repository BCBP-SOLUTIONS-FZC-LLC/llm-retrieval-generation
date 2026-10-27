"""Outbound event port shared by every module (§12.3, EVT-8, OBX-1)."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol
from uuid import UUID

from rag.application.shared.ports import TxHandle


@dataclass(frozen=True, slots=True)
class DomainEvent:
    """An event to publish: PascalCase ``type`` (LD-49), the owning tenant, payload and subject.

    ``subject`` is ``tenders/<tender_id>`` where Tender-bound and ``""`` otherwise (LD-27).

    The payload carries identifiers only, never content (EVT-7).
    """

    type: str
    tenant_id: UUID
    subject: str
    payload: dict[str, Any] = field(default_factory=dict)
    correlation_id: str = ""
    """Propagated from the originating request or the run's input; ``""`` leaves it off the wire."""


class EventPublisher(Protocol):
    """Enqueues an event on the connection of the state change that caused it (EVT-8).

    Nothing is sent here: delivery is the outbox relay's job. ``tx`` must be the handle the
    use case's ``TransactionRunner`` passed in, so the event commits or rolls back with it.
    """

    async def publish(self, tx: TxHandle, event: DomainEvent) -> None:
        """Enqueue ``event`` in the caller's transaction."""
        ...
