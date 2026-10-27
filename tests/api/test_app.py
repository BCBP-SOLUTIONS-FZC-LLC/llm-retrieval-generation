"""API tests: /health, the identity-header middleware, auth-declaration check, error mapping."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any
from uuid import UUID

import pytest
from fastapi import Depends, FastAPI
from fastapi.dependencies.models import Dependant
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

from rag.domain.shared.errors import DomainError, ErrorCode
from rag.domain.shared.identity import RequestIdentity, ResidencyMode
from rag.infrastructure.config import Settings
from rag.presentation.identity import (
    get_extra_identity,
    parse_departments,
    require_identity,
    require_tenant,
)
from rag.presentation.main import create_app

TENANT = "0a3f0000-0000-7000-8000-000000000001"
USER = "0a3f0000-0000-7000-8000-000000000002"
DEPT = "3fa85f64-5717-4562-b3fc-2c963f66afa6"
PLATFORM_ROUTES = {"/health", "/live", "/ready", "/metrics"}
C_M1 = "/api/internal/models"  # API-11 / CI-1 exempt it by name (mTLS only, no tenant)


@pytest.fixture
def app(settings: Settings) -> FastAPI:
    """App with a probe route per auth style and one that raises each error shape."""
    app = create_app(settings)

    @app.get("/probe/public")
    async def public(identity: RequestIdentity = Depends(require_identity)) -> dict[str, Any]:
        return {
            "tenant": str(identity.tenant_id),
            "principal": str(identity.principal_id),
            "roles": sorted(identity.roles),
            "departments": [(str(d.department_id), d.role) for d in identity.departments],
            "plan": identity.plan,
            "flags": sorted(identity.feature_flags),
            "residency": identity.residency_mode,
            "read_only": identity.read_only,
        }

    @app.get("/probe/internal")
    async def internal(tenant: UUID = Depends(require_tenant)) -> dict[str, str]:
        return {"tenant": str(tenant)}

    @app.get("/probe/extra")
    async def extra() -> dict[str, Any]:
        return {"plan": get_extra_identity().plan}

    @app.get("/probe/domain-error")
    async def domain_error() -> None:
        raise DomainError(
            ErrorCode.OPTIMISTIC_LOCK_CONFLICT, "Stale.", extensions={"recordVersion": 7}
        )

    @app.get("/probe/quota")
    async def quota() -> None:
        raise DomainError(ErrorCode.QUOTA_EXHAUSTED, retry_after_seconds=12)

    return app


@pytest.fixture
def client(app: FastAPI) -> Iterator[TestClient]:
    """Client that does not run the lifespan (no databases)."""
    yield TestClient(app)


def test_health(client: TestClient) -> None:
    """The library's liveness route answers without a database."""
    r = client.get("/health")
    assert (r.status_code, r.json()) == (200, {"status": "ok"})


def test_extra_identity_headers_parsed(client: TestClient) -> None:
    """x-departments / x-plan / x-feature-flags / x-residency-mode reach the handler (API-12)."""
    r = client.get(
        "/probe/public",
        headers={
            "x-tenant-id": TENANT,
            "x-user-id": USER,
            "x-tenant-roles": "tenant_admin",
            "x-departments": f"{DEPT}:approver, not-a-uuid:viewer,{DEPT}:",
            "x-plan": "pro",
            "x-feature-flags": "read_only, beta",
            "x-residency-mode": "strict",
        },
    )
    assert r.status_code == 200
    assert r.json() == {
        "tenant": TENANT,
        "principal": USER,
        "roles": ["tenant_admin"],
        "departments": [[DEPT, "approver"]],
        "plan": "pro",
        "flags": ["beta", "read_only"],
        "residency": "strict",
        "read_only": True,
    }


def test_extra_identity_defaults(client: TestClient) -> None:
    """Absent headers: no departments, no plan, standard residency, not read-only."""
    r = client.get("/probe/public", headers={"x-tenant-id": TENANT, "x-user-id": USER})
    assert r.json()["residency"] == ResidencyMode.STANDARD
    assert (r.json()["plan"], r.json()["departments"], r.json()["read_only"]) == (None, [], False)


def test_unknown_residency_is_strict(client: TestClient) -> None:
    """An unknown x-residency-mode means strict (LD-74)."""
    r = client.get(
        "/probe/public",
        headers={"x-tenant-id": TENANT, "x-user-id": USER, "x-residency-mode": "eu-only"},
    )
    assert r.json()["residency"] == "strict"


def test_repeated_plan_header_is_ambiguous_and_dropped(client: TestClient) -> None:
    """A repeated single-value header resolves as absent."""
    r = client.get("/probe/extra", headers=[("x-plan", "pro"), ("x-plan", "starter")])
    assert r.json() == {"plan": None}


def test_carrier_does_not_leak_between_requests(client: TestClient) -> None:
    """The ContextVar is reset after each request."""
    assert client.get("/probe/extra", headers={"x-plan": "pro"}).json() == {"plan": "pro"}
    assert client.get("/probe/extra").json() == {"plan": None}


def test_public_route_requires_user(client: TestClient) -> None:
    """require_identity answers 401 problem+json without x-user-id."""
    r = client.get("/probe/public", headers={"x-tenant-id": TENANT})
    assert r.status_code == 401
    assert r.headers["content-type"].startswith("application/problem+json")
    assert r.json()["errorCode"] == "UNAUTHORIZED"


@pytest.mark.parametrize(
    "headers",
    [
        [],
        [("x-tenant-id", "not-a-uuid")],
        [("x-tenant-id", TENANT), ("x-tenant-id", TENANT)],
    ],
)
def test_internal_route_needs_one_uuid_tenant(
    client: TestClient, headers: list[tuple[str, str]]
) -> None:
    """require_tenant: absent, malformed or repeated x-tenant-id is 401 (LD-95)."""
    assert client.get("/probe/internal", headers=headers).status_code == 401


def test_internal_route_accepts_tenant_without_user(client: TestClient) -> None:
    """An internal caller sends only x-tenant-id."""
    r = client.get("/probe/internal", headers={"x-tenant-id": TENANT})
    assert (r.status_code, r.json()) == (200, {"tenant": TENANT})


def test_domain_error_renders_problem_with_extension(client: TestClient) -> None:
    """A DomainError becomes its §24 problem, type URI under the configured base."""
    r = client.get("/probe/domain-error", headers={"x-tenant-id": TENANT, "x-user-id": USER})
    body = r.json()
    assert r.status_code == 409
    assert body["errorCode"] == "OPTIMISTIC_LOCK_CONFLICT"
    assert body["type"] == "https://errors.bcbp.io/retrieval-generation/optimistic-lock-conflict"
    assert (body["title"], body["detail"], body["recordVersion"]) == (
        "Optimistic lock conflict",
        "Stale.",
        7,
    )
    assert r.headers["content-type"].startswith("application/problem+json")


def test_quota_exhausted_sets_retry_after(client: TestClient) -> None:
    """429 QUOTA_EXHAUSTED carries Retry-After."""
    r = client.get("/probe/quota")
    assert (r.status_code, r.headers["retry-after"]) == (429, "12")


def test_parse_departments_drops_malformed() -> None:
    """Malformed pairs are dropped, valid ones kept in order."""
    assert [d.role for d in parse_departments(f"{DEPT}:a,x:b,{DEPT}:c")] == ["a", "c"]


def _calls(dependant: Dependant) -> Iterator[Any]:
    for dep in dependant.dependencies:
        yield dep.call
        yield from _calls(dep)


def _api_routes(app: FastAPI) -> Iterator[tuple[str, Dependant]]:
    """``(path, dependant)`` of every API route, including those mounted by ``include_router``.

    FastAPI 0.16x keeps an included router as one opaque ``_IncludedRouter`` entry in
    ``app.routes``, so a plain ``isinstance(r, APIRoute)`` scan sees none of them and the
    check below would pass vacuously.
    """
    for r in app.routes:
        if isinstance(r, APIRoute):
            yield r.path, r.dependant
        elif hasattr(r, "effective_route_contexts"):
            for ctx in r.effective_route_contexts():
                if isinstance(ctx.original_route, APIRoute):
                    yield ctx.path, ctx.original_route.dependant


def anonymous_routes(app: FastAPI) -> list[str]:
    """CI-1 / API-11: routes (outside the library's own) with no authentication dependency."""
    from fastapicommon.auth import require_authenticated_user

    auth = {require_authenticated_user, require_tenant}
    return [
        path
        for path, dependant in _api_routes(app)
        if path not in PLATFORM_ROUTES and not auth & set(_calls(dependant))
    ]


def test_ci1_every_route_declares_authentication(app: FastAPI) -> None:
    """The probe routes with auth pass; the checker flags the anonymous ones."""
    assert set(anonymous_routes(app)) == {
        "/probe/extra",
        "/probe/domain-error",
        "/probe/quota",
        C_M1,
    }


def test_ci1_sees_routes_mounted_by_include_router(app: FastAPI) -> None:
    """The scan reaches included routers: the corpus routes are found, and all carry auth."""
    paths = {path for path, _ in _api_routes(app)}
    assert {"/api/v1/collections", "/api/v1/internal/collections/resolve", C_M1} <= paths


def test_ci1_clean_app_has_no_anonymous_routes(settings: Settings) -> None:
    """A fresh app has only the library's exempt routes, and C-M1 (named in API-11)."""
    assert anonymous_routes(create_app(settings)) == [C_M1]
