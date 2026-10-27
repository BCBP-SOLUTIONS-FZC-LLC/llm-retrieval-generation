"""Event shape: payload builders (§12.3 ``llm.index.events``) and the outbox envelope."""

from __future__ import annotations

import json
from typing import Any
from uuid import UUID

import pytest
from tests.support_corpus import NOW, TENANT, TENDER, World

from rag.application.corpus.commands import ResolveCollectionCommand
from rag.application.corpus.events import (
    INDEX_EVENT_TYPES,
    StatusReason,
    collection_promoted,
    collection_provisioned,
    collection_status_changed,
    index_generation_activated,
)
from rag.application.corpus.lifecycle import ApplyTenantState, RestrictTender
from rag.application.corpus.resolve import ResolveOrCreateCollection
from rag.application.shared.events import DomainEvent
from rag.domain.corpus.collection import Collection
from rag.domain.corpus.index_generation import IndexGeneration
from rag.domain.shared.enums import IndexGenerationStatus as G
from rag.infrastructure.events.publisher import EVENT_SOURCE, OutboxEventPublisher


class FakeConn:
    """Stands in for an asyncpg connection inside a transaction."""

    def __init__(self, in_tx: bool = True) -> None:
        self.in_tx = in_tx
        self.rows: list[tuple[Any, ...]] = []

    def is_in_transaction(self) -> bool:
        return self.in_tx

    async def execute(self, sql: str, *args: Any) -> str:
        self.rows.append(args)
        return "INSERT 0 1"


async def test_publisher_enqueues_a_canonical_envelope_on_the_callers_connection() -> None:
    """EVT-8: id, type, body, tenant, trace id, ordering key reach ``outbox_events`` (§12.2)."""
    conn = FakeConn()
    c = _collection()
    event = collection_provisioned(c)
    await OutboxEventPublisher().publish(conn, event)
    [(event_id, type_, body, tenant, _trace, ordering)] = conn.rows
    assert type_ == "CollectionProvisioned" and tenant == str(TENANT) and ordering == ""
    assert str(UUID(event_id)) == event_id
    wire = json.loads(body)
    assert wire["id"] == event_id and wire["type"] == "CollectionProvisioned"
    assert wire["source"] == EVENT_SOURCE == "llm-rag-service"
    assert wire["subject"] == f"tenders/{TENDER}" and wire["tenant_id"] == str(TENANT)
    assert wire["data"] == event.payload


async def test_publisher_refuses_a_connection_outside_a_transaction() -> None:
    """The event must commit with the state change: eventcommon rejects a bare connection."""
    with pytest.raises(ValueError, match="transaction"):
        await OutboxEventPublisher().publish(
            FakeConn(in_tx=False), collection_provisioned(_collection())
        )


async def test_publisher_refuses_an_unregistered_type_or_a_payload_off_its_schema() -> None:
    """EVT-9: fail closed in the state change's transaction, nothing reaches the outbox."""
    conn = FakeConn()
    with pytest.raises(ValueError, match="not a registered"):
        await OutboxEventPublisher().publish(conn, DomainEvent("Nope", TENANT, "", {}))
    with pytest.raises(ValueError):
        await OutboxEventPublisher().publish(
            conn, DomainEvent("IndexGenerationActivated", TENANT, "", {"generation": 13})
        )
    assert conn.rows == []


def _collection(**kw: Any) -> Collection:
    return Collection.create(tenant_id=TENANT, tender_id=TENDER, restricted=False, **kw)


def test_index_generation_activated_payload_matches_the_lld_example() -> None:
    """§12.3: collection, tender, generation, previous, profile, chunker, activated_at."""
    c = _collection()
    g = IndexGeneration(
        c.id, 13, TENANT, "emb-m3-1024", "c-1.9.0", G.ACTIVE, NOW, NOW, activated_at=NOW
    )
    event = index_generation_activated(c, g, 12)
    assert event.type == "IndexGenerationActivated" and event.subject == f"tenders/{TENDER}"
    assert event.payload == {
        "collection_id": str(c.id),
        "tender_id": str(TENDER),
        "generation": 13,
        "previous_generation": 12,
        "embedding_profile": "emb-m3-1024",
        "chunker_version": "c-1.9.0",
        "activated_at": "2026-04-02T09:14:00Z",
    }
    json.dumps(event.payload)  # JSON-serialisable: identifiers and scalars only (EVT-7)


def test_collection_events_match_the_lld_examples() -> None:
    """CollectionProvisioned / CollectionPromoted / CollectionStatusChanged field sets."""
    c = _collection()
    assert set(collection_provisioned(c).payload) == {
        "collection_id",
        "tender_id",
        "type",
        "restricted",
        "promoted_from",
        "status",
    }
    assert collection_provisioned(c).payload["promoted_from"] is None
    target = Collection.create(
        tenant_id=TENANT, tender_id=TENDER, restricted=True, promoted_from=c.id
    )
    promoted = collection_promoted(target).payload
    assert promoted["promoted_from"] == str(c.id) and promoted["restricted"] is True
    changed = collection_status_changed(c, c.activate(), StatusReason.PROMOTION_CUTOVER).payload
    assert changed == {
        "collection_id": str(c.id),
        "tender_id": str(TENDER),
        "type": "tender",
        "from_status": "provisioning",
        "to_status": "active",
        "reason": "promotion_cutover",
    }


def test_status_reasons_are_the_lld_five() -> None:
    """LD-78."""
    assert {r.value for r in StatusReason} == {
        "tenant_suspended",
        "tenant_reactivated",
        "purge_started",
        "promotion_cutover",
        "migration_failed",
    }


async def test_every_event_a_use_case_publishes_is_routed_to_the_index_topic() -> None:
    """§18.3: the relay routes these PascalCase names to llm-index-events."""
    w = World()
    await ResolveOrCreateCollection(w.tx, w.repos, w.events).execute(
        ResolveCollectionCommand(TENANT, TENDER)
    )
    await RestrictTender(w.tx, w.repos, w.events).execute(TENANT, TENDER)
    await ApplyTenantState(w.tx, w.repos, w.events).execute(TENANT, "suspended")
    assert w.state.outbox
    assert {e.type for e in w.state.outbox} <= INDEX_EVENT_TYPES
