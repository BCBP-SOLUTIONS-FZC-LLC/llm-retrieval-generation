"""Corpus routes through the real app with fake repositories: statuses, bodies, problem codes."""

from __future__ import annotations

from typing import Any
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from tests.support_corpus import (
    NOW,
    TENANT,
    TENDER,
    FakeCatalogue,
    FakePoints,
    FakeScope,
    World,
)

from rag.domain.corpus.tenant_lifecycle import TenantLifecycle, TenantState
from rag.domain.shared.enums import CollectionStatus as C
from rag.domain.shared.enums import IndexGenerationStatus as G
from rag.domain.shared.errors import DomainError, ErrorCode
from rag.infrastructure.config import Settings
from rag.presentation.corpus_dependencies import build_corpus_services, get_corpus_services
from rag.presentation.main import create_app

INTERNAL = {"x-tenant-id": str(TENANT)}
USER = "0a3f0000-0000-7000-8000-000000000002"


def public(*roles: str) -> dict[str, str]:
    """Gateway-injected identity headers."""
    return {"x-tenant-id": str(TENANT), "x-user-id": USER, "x-tenant-roles": ",".join(roles)}


class Harness:
    """An app over one fake world, with a settable visible scope."""

    def __init__(self, settings: Settings) -> None:
        self.world = World()
        self.scope = FakeScope()
        self.points = FakePoints(0)
        self.catalogue: Any = FakeCatalogue()
        self.app: FastAPI = create_app(settings)
        self.client = TestClient(self.app)
        self.app.dependency_overrides[get_corpus_services] = lambda: build_corpus_services(
            settings,
            self.world.tx,
            repos=self.world.repos,
            events=self.world.events,
            scope=self.scope,
            points=self.points,
            catalogue=self.catalogue,
        )


@pytest.fixture
def h(settings: Settings) -> Harness:
    """A fresh harness."""
    return Harness(settings)


# --- C-I1 / C-I2 ----------------------------------------------------------------------------


def test_resolve_201_then_200_with_the_same_body(h: Harness) -> None:
    """C-I1: this call created it (201), then found it (200); same body, no 404 ever."""
    body = {"tender_id": str(TENDER)}
    first = h.client.post("/api/v1/internal/collections/resolve", json=body, headers=INTERNAL)
    second = h.client.post("/api/v1/internal/collections/resolve", json=body, headers=INTERNAL)
    assert (first.status_code, second.status_code) == (201, 200)
    assert first.json() == second.json()
    assert set(first.json()) == {
        "collection_id",
        "physical_collection",
        "shard_key",
        "type",
        "knowledge_level",
        "status",
        "active_index_generation",
        "purge_epoch",
        "restricted",
        "record_version",
    }
    assert first.json()["status"] == "provisioning" and first.json()["knowledge_level"] == "TENDER"


def test_resolve_takes_the_tenant_from_the_header_never_the_body(h: Harness) -> None:
    """API-1: a body tenant_id is ignored."""
    r = h.client.post(
        "/api/v1/internal/collections/resolve",
        json={"tender_id": str(TENDER), "tenant_id": str(uuid4())},
        headers=INTERNAL,
    )
    assert r.status_code == 201
    assert {c.tenant_id for c in h.world.state.collections.values()} == {TENANT}


@pytest.mark.parametrize(
    ("setup", "status", "code"),
    [
        ("frozen_tenant", 409, "COLLECTION_FROZEN"),
        ("purged_tenant", 410, "COLLECTION_PURGING"),
        ("purging_row", 410, "COLLECTION_PURGING"),
    ],
)
def test_resolve_refusals(h: Harness, setup: str, status: int, code: str) -> None:
    """TL-3 / COL-13 problems carry their own codes."""
    if setup == "frozen_tenant":
        h.world.state.lifecycle[TENANT] = TenantLifecycle(TENANT, TenantState.FROZEN, NOW)
    elif setup == "purged_tenant":
        h.world.state.lifecycle[TENANT] = TenantLifecycle(TENANT, TenantState.PURGED, NOW)
    else:
        h.world.add_collection(status=C.PURGING)
    r = h.client.post(
        "/api/v1/internal/collections/resolve", json={"tender_id": str(TENDER)}, headers=INTERNAL
    )
    assert (r.status_code, r.json()["errorCode"]) == (status, code)
    assert r.headers["content-type"].startswith("application/problem+json")


def test_resolve_reference_is_409_not_provisioned(h: Harness) -> None:
    """COL-3."""
    r = h.client.post(
        "/api/v1/internal/collections/resolve",
        json={"tender_id": "00000000-0000-7000-8000-4c4c4d000002"},
        headers=INTERNAL,
    )
    assert (r.status_code, r.json()["errorCode"]) == (409, "COLLECTION_NOT_PROVISIONED")


def test_resolve_validation_and_auth(h: Harness) -> None:
    """tender_id absent is 400 VALIDATION_FAILED; a missing or bad x-tenant-id is 401."""
    url = "/api/v1/internal/collections/resolve"
    assert h.client.post(url, json={}, headers=INTERNAL).json()["errorCode"] == "VALIDATION_FAILED"
    assert h.client.post(url, json={"tender_id": "nope"}, headers=INTERNAL).status_code == 400
    assert h.client.post(url, json={"tender_id": str(TENDER)}).status_code == 401
    assert (
        h.client.post(
            url, json={"tender_id": str(TENDER)}, headers={"x-tenant-id": "x"}
        ).status_code
        == 401
    )


def test_resolve_store_unreachable_is_503(h: Harness) -> None:
    """§24 SCOPE_STORE_UNAVAILABLE renders as 503."""

    async def down(*_: Any, **__: Any) -> Any:
        raise DomainError(ErrorCode.SCOPE_STORE_UNAVAILABLE, "down")

    h.world.tx.run_in_tx = down  # type: ignore[method-assign]
    r = h.client.post(
        "/api/v1/internal/collections/resolve", json={"tender_id": str(TENDER)}, headers=INTERNAL
    )
    assert (r.status_code, r.json()["errorCode"]) == (503, "SCOPE_STORE_UNAVAILABLE")


def test_placement_any_status_and_404(h: Harness) -> None:
    """C-I2 answers purging; unknown is COLLECTION_NOT_FOUND."""
    c = h.world.add_collection(status=C.PURGING)
    ok = h.client.get(f"/api/v1/internal/collections/{c.id}", headers=INTERNAL)
    assert (ok.status_code, ok.json()["status"]) == (200, "purging")
    missing = h.client.get(f"/api/v1/internal/collections/{uuid4()}", headers=INTERNAL)
    assert (missing.status_code, missing.json()["errorCode"]) == (404, "COLLECTION_NOT_FOUND")
    assert h.client.get(f"/api/v1/internal/collections/{c.id}").status_code == 401


# --- C-I4 / C-I5 / C-I6 ---------------------------------------------------------------------


def _activate(h: Harness, c: Any, gen: int, version: int) -> Any:
    return h.client.post(
        f"/api/v1/internal/collections/{c.id}/index-generations/{gen}/activate",
        json={"record_version": version},
        headers=INTERNAL,
    )


def test_activate_200_body_and_409s(h: Harness) -> None:
    """C-I4: body fields incl. null previous; NOT_VERIFIED; stale version carries recordVersion."""
    c = h.world.add_collection(status=C.PROVISIONING)
    h.world.add_generation(c, 2, G.BUILDING)
    not_verified = _activate(h, c, 2, 1)
    assert (not_verified.status_code, not_verified.json()["errorCode"]) == (
        409,
        "GENERATION_NOT_VERIFIED",
    )
    stale = _activate(h, c, 1, 7)
    assert (stale.status_code, stale.json()["errorCode"], stale.json()["recordVersion"]) == (
        409,
        "OPTIMISTIC_LOCK_CONFLICT",
        1,
    )
    ok = _activate(h, c, 1, 1)
    assert (ok.status_code, ok.json()) == (
        200,
        {"active_index_generation": 1, "previous": None, "record_version": 2},
    )
    repeat = _activate(h, c, 1, 999)
    assert (repeat.status_code, repeat.json()["active_index_generation"]) == (200, 1)
    assert _activate(h, c, 9, 1).json()["errorCode"] == "COLLECTION_NOT_FOUND"
    assert (
        h.client.post(
            f"/api/v1/internal/collections/{c.id}/index-generations/1/activate",
            json={},
            headers=INTERNAL,
        ).status_code
        == 400
    )


def test_retention_read_and_purge_epoch(h: Harness) -> None:
    """C-I5 five fields; C-I6 returns the new epoch and takes no body."""
    c = h.world.add_collection()
    h.scope.ids = (c.id,)
    h.client.put(
        f"/api/v1/collections/{c.id}/retention",
        json={"retain_days": 30},
        headers=public("tenant_admin"),
    )
    r = h.client.get(f"/api/v1/internal/collections/{c.id}/retention", headers=INTERNAL)
    assert set(r.json()) == {
        "collection_id",
        "retain_days",
        "legal_hold",
        "record_version",
        "updated_at",
    }
    assert h.client.post(
        f"/api/v1/internal/collections/{c.id}/purge-epoch", headers=INTERNAL
    ).json() == {"purge_epoch": 1}
    assert (
        h.client.post(
            f"/api/v1/internal/collections/{uuid4()}/purge-epoch", headers=INTERNAL
        ).status_code
        == 404
    )


# --- C-I8 -----------------------------------------------------------------------------------


def _migration(h: Harness) -> tuple[Any, Any, str]:
    src = h.world.add_collection(status=C.ACTIVE)
    mid = uuid4()
    tgt = h.world.add_collection(
        restricted=True, status=C.PROVISIONING, promoted_from=src.id, migration_id=mid
    )
    return src, tgt, str(mid)


def test_migration_result_bodies_and_errors(h: Harness) -> None:
    """C-I8: 400 on a missing field, 409 mismatch, 409 incomplete (with counts), 200 cutover."""
    _, tgt, mid = _migration(h)
    url = f"/api/v1/internal/collections/{tgt.id}/migration-result"
    assert (
        h.client.post(
            url, json={"migration_id": mid, "result": "complete"}, headers=INTERNAL
        ).status_code
        == 400
    )
    assert (
        h.client.post(
            url, json={"migration_id": mid, "result": "failed"}, headers=INTERNAL
        ).status_code
        == 400
    )
    mismatch = h.client.post(
        url,
        json={"migration_id": str(uuid4()), "result": "complete", "points_copied": 5},
        headers=INTERNAL,
    )
    assert (mismatch.status_code, mismatch.json()["errorCode"]) == (409, "MIGRATION_MISMATCH")
    h.points.count = 10
    short = h.client.post(
        url, json={"migration_id": mid, "result": "complete", "points_copied": 4}, headers=INTERNAL
    )
    assert (short.status_code, short.json()["errorCode"]) == (409, "MIGRATION_INCOMPLETE")
    assert (short.json()["pointsExpected"], short.json()["pointsReported"]) == (10, 4)
    done = h.client.post(
        url, json={"migration_id": mid, "result": "complete", "points_copied": 10}, headers=INTERNAL
    )
    assert (done.status_code, done.json()["status"]) == (200, "active")
    assert set(done.json()) == {"collection_id", "status", "record_version"}


def test_migration_failed_result(h: Harness) -> None:
    """C-I8 failed: target to migration_failed."""
    _, tgt, mid = _migration(h)
    r = h.client.post(
        f"/api/v1/internal/collections/{tgt.id}/migration-result",
        json={"migration_id": mid, "result": "failed", "reason": "qdrant_write_timeout"},
        headers=INTERNAL,
    )
    assert (r.status_code, r.json()["status"]) == (200, "migration_failed")


# --- C-M1 -----------------------------------------------------------------------------------


def test_models_needs_no_headers_and_has_the_lld_shape(h: Harness) -> None:
    """C-M1: mTLS only, no tenant, no 401."""
    r = h.client.get("/api/internal/models")
    assert r.status_code == 200
    assert r.json()["models"] == [
        {"id": "gen-large-eu", "residency": "eu", "task_classes": ["draft"], "plans": ["pro"]}
    ]
    assert r.json()["as_of"].endswith("Z")


def test_models_unreadable_catalogue_is_503_inference_unavailable(h: Harness) -> None:
    """C-M1: the Gateway's catalogue cannot be read."""

    class Down:
        async def list_models(self) -> Any:
            raise DomainError(ErrorCode.INFERENCE_UNAVAILABLE, "no catalogue")

    h.catalogue = Down()
    r = h.client.get("/api/internal/models")
    assert (r.status_code, r.json()["errorCode"]) == (503, "INFERENCE_UNAVAILABLE")


# --- P-1 / P-2 ------------------------------------------------------------------------------


def test_p1_lists_scope_resolved_collections_without_physical_names(h: Harness) -> None:
    """P-1: 200 with an empty list for an empty scope; no placement leaks to a tenant caller."""
    c = h.world.add_collection(status=C.ACTIVE)
    empty = h.client.get("/api/v1/collections", headers=public())
    assert (empty.status_code, empty.json()["collections"]) == (200, [])
    h.scope.ids = (c.id,)
    r = h.client.get("/api/v1/collections", headers=public())
    [item] = r.json()["collections"]
    assert item["id"] == str(c.id) and item["record_version"] == 1
    assert "physical_collection" not in item and "shard_key" not in item
    assert (r.json()["limit"], r.json()["offset"]) == (100, 0)


def test_p1_pagination_rules(h: Harness) -> None:
    """A larger limit is clamped; non-positive limit and negative offset are 400 (§5.1)."""
    assert h.client.get("/api/v1/collections?limit=9999", headers=public()).json()["limit"] == 500
    assert h.client.get("/api/v1/collections?limit=0", headers=public()).status_code == 400
    assert h.client.get("/api/v1/collections?offset=-1", headers=public()).status_code == 400
    assert h.client.get("/api/v1/collections?limit=x", headers=public()).status_code == 400


def test_p1_p2_require_authentication(h: Harness) -> None:
    """API-11: public routes answer 401 without x-user-id."""
    assert (
        h.client.get("/api/v1/collections", headers={"x-tenant-id": str(TENANT)}).status_code == 401
    )
    assert h.client.get(f"/api/v1/collections/{uuid4()}").status_code == 401


def test_p2_not_in_scope_is_404(h: Harness) -> None:
    """CG-8: absent and ungranted are the same 404."""
    c = h.world.add_collection()
    r = h.client.get(f"/api/v1/collections/{c.id}", headers=public())
    assert (r.status_code, r.json()["errorCode"]) == (404, "COLLECTION_NOT_FOUND")
    h.scope.ids = (c.id,)
    assert h.client.get(f"/api/v1/collections/{c.id}", headers=public()).status_code == 200


# --- P-3 / P-6 ------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("roles", "status"),
    [((), 403), (("tender_admin",), 200), (("tenant_admin",), 200), (("user",), 403)],
)
def test_p3_role_gate(h: Harness, roles: tuple[str, ...], status: int) -> None:
    """P-3: tenant_admin or tender_admin; anyone else is 403 FORBIDDEN."""
    c = h.world.add_collection()
    h.scope.ids = (c.id,)
    r = h.client.get(f"/api/v1/collections/{c.id}/grants", headers=public(*roles))
    assert r.status_code == status
    if status == 200:
        assert r.json()["authoritative"] is False
        assert r.json()["projection_freshness"] == {"as_of": None, "bound_seconds": 60}
        assert r.json()["entries"] == [] and (r.json()["limit"], r.json()["offset"]) == (100, 0)
    else:
        assert r.json()["errorCode"] == "FORBIDDEN"


def test_p3_no_write_methods_exist(h: Harness) -> None:
    """CG-2: no POST, PUT or DELETE under the grants path."""
    c = h.world.add_collection()
    url = f"/api/v1/collections/{c.id}/grants"
    for method in ("post", "put", "delete", "patch"):
        r = getattr(h.client, method)(url, headers=public("tenant_admin"))
        assert r.status_code == 405


def test_p3_out_of_scope_is_404(h: Harness) -> None:
    """The collection is found through scope resolution."""
    c = h.world.add_collection()
    r = h.client.get(f"/api/v1/collections/{c.id}/grants", headers=public("tenant_admin"))
    assert r.status_code == 404


def test_p6_create_update_conflict_and_role(h: Harness) -> None:
    """P-6: 201 first write, 200 versioned update, 409 with recordVersion, tenant_admin only."""
    c = h.world.add_collection()
    h.scope.ids = (c.id,)
    url = f"/api/v1/collections/{c.id}/retention"
    admin = public("tenant_admin")

    assert (
        h.client.put(url, json={"retain_days": 30}, headers=public("tender_admin")).status_code
        == 403
    )
    created = h.client.put(url, json={"retain_days": 30}, headers=admin)
    assert (created.status_code, created.json()["applied"], created.json()["record_version"]) == (
        201,
        False,
        1,
    )

    dup = h.client.put(url, json={"retain_days": 60}, headers=admin)
    assert (dup.status_code, dup.json()["errorCode"], dup.json()["recordVersion"]) == (
        409,
        "OPTIMISTIC_LOCK_CONFLICT",
        1,
    )
    upd = h.client.put(
        url, json={"retain_days": 60, "legal_hold": True, "record_version": 1}, headers=admin
    )
    assert (
        upd.status_code,
        upd.json()["record_version"],
        upd.json()["legal_hold"],
        upd.json()["applied"],
    ) == (
        200,
        2,
        True,
        False,
    )
    stale = h.client.put(url, json={"retain_days": 90, "record_version": 1}, headers=admin)
    assert (stale.status_code, stale.json()["recordVersion"]) == (409, 2)


def test_p6_validation(h: Harness) -> None:
    """retain_days must be positive; a body is required."""
    c = h.world.add_collection()
    h.scope.ids = (c.id,)
    url = f"/api/v1/collections/{c.id}/retention"
    assert (
        h.client.put(url, json={"retain_days": 0}, headers=public("tenant_admin")).status_code
        == 400
    )
    assert h.client.put(url, json={}, headers=public("tenant_admin")).status_code == 400
    assert (
        h.client.put(url, json={"retain_days": 5}, headers={"x-tenant-id": str(TENANT)}).status_code
        == 401
    )


def test_every_corpus_route_is_declared_once() -> None:
    """The route table has the eleven §5.4 corpus operations."""
    app = create_app(Settings(environment="test", enable_tracing=False, enable_metrics=False))
    seen: set[tuple[str, str]] = set()
    for path, methods in _paths(app):
        seen |= {(m, path) for m in methods}
    expected: set[tuple[str, str]] = {
        ("GET", "/api/v1/collections"),
        ("GET", "/api/v1/collections/{collection_id}"),
        ("GET", "/api/v1/collections/{collection_id}/grants"),
        ("PUT", "/api/v1/collections/{collection_id}/retention"),
        ("POST", "/api/v1/internal/collections/resolve"),
        ("GET", "/api/v1/internal/collections/{collection_id}"),
        (
            "POST",
            "/api/v1/internal/collections/{collection_id}/index-generations/{generation}/activate",
        ),
        ("GET", "/api/v1/internal/collections/{collection_id}/retention"),
        ("POST", "/api/v1/internal/collections/{collection_id}/purge-epoch"),
        ("POST", "/api/v1/internal/collections/{collection_id}/migration-result"),
        ("GET", "/api/internal/models"),
    }
    assert expected <= seen


def _paths(app: FastAPI) -> list[tuple[str, set[str]]]:
    spec = app.openapi()["paths"]
    return [(p, {m.upper() for m in ops}) for p, ops in spec.items()]
