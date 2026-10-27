"""Builders for the ``llm.index.events`` payloads of §12.3. Identifiers and status only (EVT-7)."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from rag.application.events.contracts import INDEX_EVENT_TYPES, tender_subject
from rag.application.shared.events import DomainEvent
from rag.domain.corpus.collection import Collection
from rag.domain.corpus.index_generation import IndexGeneration

__all__ = ["INDEX_EVENT_TYPES", "StatusReason"]


class StatusReason(StrEnum):
    """``CollectionStatusChanged.reason`` (LD-78)."""

    TENANT_SUSPENDED = "tenant_suspended"
    TENANT_REACTIVATED = "tenant_reactivated"
    PURGE_STARTED = "purge_started"
    PROMOTION_CUTOVER = "promotion_cutover"
    MIGRATION_FAILED = "migration_failed"


def _iso(at: datetime | None) -> str | None:
    return None if at is None else at.isoformat().replace("+00:00", "Z")


def collection_provisioned(c: Collection) -> DomainEvent:
    """C-I1's and the ``TenderRestricted`` consumer's creation."""
    return DomainEvent(
        "CollectionProvisioned",
        c.tenant_id,
        tender_subject(c.tender_id),
        {
            "collection_id": str(c.id),
            "tender_id": str(c.tender_id),
            "type": c.type.value,
            "restricted": c.restricted,
            "promoted_from": None,
            "status": c.status.value,
        },
    )


def collection_promoted(target: Collection) -> DomainEvent:
    """A promotion target inserted by the ``TenderRestricted`` consumer."""
    return DomainEvent(
        "CollectionPromoted",
        target.tenant_id,
        tender_subject(target.tender_id),
        {
            "collection_id": str(target.id),
            "tender_id": str(target.tender_id),
            "type": target.type.value,
            "restricted": True,
            "promoted_from": str(target.promoted_from),
            "status": target.status.value,
        },
    )


def collection_status_changed(
    before: Collection, after: Collection, reason: StatusReason
) -> DomainEvent:
    """Every status change other than creation (LD-78)."""
    return DomainEvent(
        "CollectionStatusChanged",
        after.tenant_id,
        tender_subject(after.tender_id),
        {
            "collection_id": str(after.id),
            "tender_id": str(after.tender_id),
            "type": after.type.value,
            "from_status": before.status.value,
            "to_status": after.status.value,
            "reason": reason.value,
        },
    )


def index_generation_activated(
    c: Collection, g: IndexGeneration, previous: int | None
) -> DomainEvent:
    """C-I4's cutover transaction (IG-1, EVT-8)."""
    return DomainEvent(
        "IndexGenerationActivated",
        c.tenant_id,
        tender_subject(c.tender_id),
        {
            "collection_id": str(c.id),
            "tender_id": str(c.tender_id),
            "generation": g.generation,
            "previous_generation": previous,
            "embedding_profile": g.embedding_profile,
            "chunker_version": g.chunker_version,
            "activated_at": _iso(g.activated_at),
        },
    )
