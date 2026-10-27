"""E2E flows: whole requests through the real app, real RLS-bound pools and the real outbox."""

from __future__ import annotations

from typing import Any

import pytest

from tests.e2e.conftest import TENANT_A, TENDER, Stack, identity, internal, new_id

pytestmark = pytest.mark.e2e

RESOLVE = "/api/v1/internal/collections/resolve"


def resolve(stack: Stack, tender: Any = TENDER, tenant: Any = TENANT_A) -> dict[str, Any]:
    """C-I1 and return the placement body."""
    r = stack.client.post(RESOLVE, json={"tender_id": str(tender)}, headers=internal(tenant))
    assert r.status_code in (200, 201), r.text
    body: dict[str, Any] = r.json()
    return body


def test_resolve_creates_then_finds_the_same_collection(stack: Stack) -> None:
    """FLOW-001 (C-I1): 201 for the creating call, 200 and an identical body afterwards."""
    first = stack.client.post(RESOLVE, json={"tender_id": str(TENDER)}, headers=internal())
    second = stack.client.post(RESOLVE, json={"tender_id": str(TENDER)}, headers=internal())
    assert (first.status_code, second.status_code) == (201, 200)
    assert first.json() == second.json()
    assert first.json()["status"] == "provisioning"
    assert first.json()["knowledge_level"] == "TENDER"


def test_resolve_persists_one_row_and_one_outbox_event(stack: Stack) -> None:
    """FLOW-002: the row and its CollectionProvisioned event commit in one transaction."""
    placement = resolve(stack)
    resolve(stack)  # idempotent: no second row, no second event
    rows = stack.sql("corpus", "SELECT id::text, tenant_id FROM collections")
    assert [(r["id"], r["tenant_id"]) for r in rows] == [(placement["collection_id"], TENANT_A)]
    events = stack.sql("corpus", "SELECT event_type FROM outbox_events")
    assert [e["event_type"] for e in events] == ["CollectionProvisioned"]


def test_internal_placement_round_trips(stack: Stack) -> None:
    """FLOW-003 (C-I2): GET placement returns what resolve returned."""
    placement = resolve(stack)
    r = stack.client.get(
        f"/api/v1/internal/collections/{placement['collection_id']}", headers=internal()
    )
    assert r.status_code == 200
    assert r.json() == placement


def test_public_list_and_get_follow_the_resolved_scope(stack: Stack) -> None:
    """FLOW-004 (P-1, P-2): an empty scope lists nothing; a visible collection is readable."""
    placement = resolve(stack)
    cid = placement["collection_id"]
    headers = identity(TENANT_A, "member")

    assert stack.client.get("/api/v1/collections", headers=headers).json()["collections"] == []

    stack.scope.ids = (_uuid(cid),)
    listed = stack.client.get("/api/v1/collections", headers=headers)
    assert listed.status_code == 200
    assert [c["id"] for c in listed.json()["collections"]] == [cid]
    assert stack.client.get(f"/api/v1/collections/{cid}", headers=headers).status_code == 200


def test_retention_put_get_and_optimistic_lock(stack: Stack) -> None:
    """FLOW-005 (P-6, C-I5): create 201, read back, update 200, stale version 409."""
    cid = resolve(stack)["collection_id"]
    stack.scope.ids = (_uuid(cid),)
    admin = identity(TENANT_A, "tenant_admin")
    put = f"/api/v1/collections/{cid}/retention"
    get = f"/api/v1/internal/collections/{cid}/retention"

    assert stack.client.get(get, headers=internal()).status_code == 404  # no policy yet
    created = stack.client.put(put, json={"retain_days": 30}, headers=admin)
    assert created.status_code == 201, created.text
    assert stack.client.get(get, headers=internal()).json()["retain_days"] == 30

    version = created.json()["record_version"]
    updated = stack.client.put(
        put, json={"retain_days": 60, "record_version": version}, headers=admin
    )
    assert updated.status_code == 200, updated.text
    stale = stack.client.put(
        put, json={"retain_days": 90, "record_version": version}, headers=admin
    )
    assert stale.status_code == 409
    (row,) = stack.sql("corpus", "SELECT retain_days FROM retention_policies")
    assert row["retain_days"] == 60


def test_retention_put_requires_the_admin_role(stack: Stack) -> None:
    """FLOW-005b: a plain member cannot set retention."""
    cid = resolve(stack)["collection_id"]
    stack.scope.ids = (_uuid(cid),)
    r = stack.client.put(
        f"/api/v1/collections/{cid}/retention",
        json={"retain_days": 30},
        headers=identity(TENANT_A, "member"),
    )
    assert r.status_code == 403


def test_purge_epoch_bump_is_atomic_and_persisted(stack: Stack) -> None:
    """FLOW-006 (C-I6): each call increments the epoch; the row shows the final value."""
    cid = resolve(stack)["collection_id"]
    path = f"/api/v1/internal/collections/{cid}/purge-epoch"
    first = stack.client.post(path, headers=internal())
    second = stack.client.post(path, headers=internal())
    assert (first.status_code, second.status_code) == (200, 200)
    assert second.json()["purge_epoch"] == first.json()["purge_epoch"] + 1
    (row,) = stack.sql("corpus", "SELECT purge_epoch FROM collections")
    assert row["purge_epoch"] == second.json()["purge_epoch"]


def test_model_catalogue_is_served_without_a_tenant(stack: Stack) -> None:
    """FLOW-007 (C-M1): mTLS-only route, no identity headers."""
    r = stack.client.get("/api/internal/models")
    assert r.status_code == 200
    assert r.json()["models"]


def _uuid(value: str) -> Any:
    from uuid import UUID

    return UUID(value)


def test_distinct_tenders_get_distinct_collections(stack: Stack) -> None:
    """FLOW-008: the placement key is (tenant, tender)."""
    a = resolve(stack, TENDER)
    b = resolve(stack, new_id())
    assert a["collection_id"] != b["collection_id"]
    assert a["physical_collection"] == b["physical_collection"]  # one physical, two shards
    assert a["shard_key"] != b["shard_key"]
