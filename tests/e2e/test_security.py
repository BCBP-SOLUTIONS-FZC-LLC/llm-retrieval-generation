"""E2E security: identity enforcement and tenant isolation by Postgres RLS (not by the app)."""

from __future__ import annotations

import pytest

from tests.e2e.conftest import TENANT_A, TENANT_B, TENDER, USER, Stack, identity, internal
from tests.e2e.test_flows import RESOLVE, resolve

pytestmark = pytest.mark.e2e

PUBLIC = "/api/v1/collections"


@pytest.mark.parametrize(
    "headers",
    [
        {},
        {"x-tenant-id": str(TENANT_A)},  # tenant without a user
        {"x-user-id": str(USER)},  # user without a tenant
        {"x-tenant-id": "not-a-uuid", "x-user-id": str(USER)},
        {"x-tenant-id": str(TENANT_A), "x-user-id": "not-a-uuid"},
    ],
    ids=["none", "tenant-only", "user-only", "bad-tenant", "bad-user"],
)
def test_public_routes_reject_missing_or_malformed_identity(
    stack: Stack, headers: dict[str, str]
) -> None:
    """SEC-001: every partial or malformed identity is a 401 problem, never a 500."""
    r = stack.client.get(PUBLIC, headers=headers)
    assert r.status_code == 401
    assert r.json()["errorCode"] == "UNAUTHORIZED"


def test_internal_routes_require_a_tenant(stack: Stack) -> None:
    """SEC-002: C-I1 without x-tenant-id is rejected and creates nothing."""
    r = stack.client.post(RESOLVE, json={"tender_id": str(TENDER)}, headers=internal(None))
    assert r.status_code in (400, 401)
    assert stack.sql("corpus", "SELECT 1 FROM collections") == []


def test_tenant_b_cannot_read_tenant_a_placement(stack: Stack) -> None:
    """SEC-003: RLS hides the row; the caller sees 404, indistinguishable from absent."""
    cid = resolve(stack)["collection_id"]
    r = stack.client.get(f"/api/v1/internal/collections/{cid}", headers=internal(TENANT_B))
    assert r.status_code == 404


def test_tenant_b_cannot_mutate_tenant_a_collection(stack: Stack) -> None:
    """SEC-004: a cross-tenant purge-epoch bump is a 404 and leaves the epoch untouched."""
    cid = resolve(stack)["collection_id"]
    r = stack.client.post(
        f"/api/v1/internal/collections/{cid}/purge-epoch", headers=internal(TENANT_B)
    )
    assert r.status_code == 404
    (row,) = stack.sql("corpus", "SELECT purge_epoch FROM collections")
    assert row["purge_epoch"] == 0


def test_public_get_is_404_for_another_tenant_even_when_the_scope_lists_it(stack: Stack) -> None:
    """SEC-005: defence in depth — a scope bug cannot leak a row past RLS."""
    placement = resolve(stack)
    from uuid import UUID

    cid = UUID(placement["collection_id"])
    stack.scope.ids = (cid,)  # a faulty/hostile scope resolver that lists it for tenant B
    r = stack.client.get(f"{PUBLIC}/{cid}", headers=identity(TENANT_B, "member"))
    assert r.status_code == 404
    listed = stack.client.get(PUBLIC, headers=identity(TENANT_B, "member"))
    assert listed.json()["collections"] == []


def test_same_tender_in_two_tenants_never_collides(stack: Stack) -> None:
    """SEC-006: tenant B resolving tenant A's tender id gets its own collection."""
    a = resolve(stack, TENDER, TENANT_A)
    b = resolve(stack, TENDER, TENANT_B)
    assert a["collection_id"] != b["collection_id"]
    assert len(stack.sql("corpus", "SELECT 1 FROM collections")) == 2


def test_app_connects_as_a_non_superuser_without_bypassrls(stack: Stack) -> None:
    """SEC-007: guards the harness itself — a superuser app role would make RLS tests vacuous."""
    rows = stack.sql(
        "corpus",
        "SELECT rolsuper, rolbypassrls FROM pg_roles WHERE rolname = 'rag_e2e_login'",
    )
    assert [(r["rolsuper"], r["rolbypassrls"]) for r in rows] == [(False, False)]
