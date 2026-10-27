"""E2E error shape: every failure is an RFC 9457 problem document from the real stack."""

from __future__ import annotations

import pytest

from tests.e2e.conftest import TENDER, Stack, identity, internal, new_id

pytestmark = pytest.mark.e2e

REQUIRED = {"type", "title", "status", "instance", "traceId", "errorCode", "timestamp"}


def assert_problem(response: object, status: int, code: str) -> dict[str, object]:
    """Assert the status, the problem content type, the required members and the code."""
    r = response  # httpx.Response
    assert r.status_code == status, r.text  # type: ignore[attr-defined]
    assert r.headers["content-type"].startswith("application/problem+json")  # type: ignore[attr-defined]
    body: dict[str, object] = r.json()  # type: ignore[attr-defined]
    assert REQUIRED <= body.keys()
    assert body["status"] == status
    assert body["errorCode"] == code
    return body


def test_unauthenticated_is_a_401_problem(stack: Stack) -> None:
    """ERR-001."""
    body = assert_problem(stack.client.get("/api/v1/collections"), 401, "UNAUTHORIZED")
    assert body["instance"] == "/api/v1/collections"


def test_malformed_path_id_is_a_400_validation_problem(stack: Stack) -> None:
    """ERR-002: names the offending field."""
    r = stack.client.get("/api/v1/internal/collections/not-a-uuid", headers=internal())
    body = assert_problem(r, 400, "VALIDATION_FAILED")
    assert body["validationErrors"][0]["field"] == "collection_id"  # type: ignore[index]


def test_malformed_body_is_a_400_validation_problem(stack: Stack) -> None:
    """ERR-003: missing required field."""
    r = stack.client.post("/api/v1/internal/collections/resolve", json={}, headers=internal())
    assert_problem(r, 400, "VALIDATION_FAILED")


def test_unknown_collection_is_a_404_problem(stack: Stack) -> None:
    """ERR-004."""
    r = stack.client.get(f"/api/v1/internal/collections/{new_id()}", headers=internal())
    assert r.status_code == 404
    assert r.headers["content-type"].startswith("application/problem+json")
    assert r.json()["status"] == 404


def test_unknown_route_is_a_404(stack: Stack) -> None:
    """ERR-005."""
    assert stack.client.get("/api/v1/nope", headers=identity()).status_code == 404


def test_workflow_engine_unconfigured_is_a_503_problem(stack: Stack) -> None:
    """ERR-006: with TEMPORAL_ADDRESS unset the async entry points fail closed."""
    r = stack.client.post(
        "/api/v1/generations",
        json={"tender_id": str(TENDER), "capability": "draft"},
        headers={**identity(), "Idempotency-Key": "e2e-1"},
    )
    assert_problem(r, 503, "WORKFLOW_ENGINE_UNAVAILABLE")
