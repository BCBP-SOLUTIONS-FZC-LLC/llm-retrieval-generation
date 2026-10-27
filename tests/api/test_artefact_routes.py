"""A-1..A-9 over HTTP: every route reaches its use case and maps a domain error to its status."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from rag.domain.shared.errors import DomainError, ErrorCode
from rag.infrastructure.config import Settings
from rag.presentation.generation_dependencies import get_generation
from rag.presentation.main import create_app

AID = uuid4()
HEADERS = {"x-tenant-id": str(uuid4()), "x-user-id": str(uuid4())}
BASE = f"/api/v1/artefacts/{AID}"

ROUTES = [
    ("get_artefact", BASE),
    ("list_versions", f"{BASE}/versions?limit=5&offset=0"),
    ("get_version", f"{BASE}/versions/2"),
    ("list_sections", f"{BASE}/sections"),
    ("render_artefact", f"{BASE}/render?version_no=1&format=pdf"),
    ("get_section_content", f"{BASE}/sections/intro/versions/1/content"),
]


def _client(settings: Settings, use_case: str, error: ErrorCode) -> tuple[TestClient, Any]:
    services = MagicMock()
    execute = AsyncMock(side_effect=DomainError(error, "x"))
    getattr(services, use_case).execute = execute
    app = create_app(settings)
    app.dependency_overrides[get_generation] = lambda: services
    return TestClient(app), execute


@pytest.mark.parametrize(("use_case", "path"), ROUTES)
def test_artefact_routes_call_their_use_case_and_map_not_found(
    settings: Settings, use_case: str, path: str
) -> None:
    """The use case runs with the caller's identity; ARTEFACT_NOT_FOUND is 404 (CG-8)."""
    client, execute = _client(settings, use_case, ErrorCode.ARTEFACT_NOT_FOUND)
    response = client.get(path, headers=HEADERS)
    assert response.status_code == 404
    assert response.json()["errorCode"] == "ARTEFACT_NOT_FOUND"
    execute.assert_awaited_once()
    assert execute.await_args.args[0].tenant_id == __import__("uuid").UUID(HEADERS["x-tenant-id"])


def test_render_is_the_reserved_503(settings: Settings) -> None:
    """A-8: RENDERER_UNAVAILABLE after the scope and artefact checks (S-6)."""
    client, _ = _client(settings, "render_artefact", ErrorCode.RENDERER_UNAVAILABLE)
    assert client.get(f"{BASE}/render", headers=HEADERS).status_code == 503


@pytest.mark.parametrize(
    "path",
    [f"{BASE}/versions/0", f"{BASE}/versions?limit=0", f"{BASE}/versions?offset=-1"],
)
def test_bad_path_and_query_values_are_rejected_before_the_use_case(
    settings: Settings, path: str
) -> None:
    """Version numbers start at 1, limits at 1, offsets at 0."""
    client, execute = _client(settings, "get_version", ErrorCode.ARTEFACT_NOT_FOUND)
    assert client.get(path, headers=HEADERS).status_code == 400
    execute.assert_not_awaited()


def test_artefact_routes_require_identity(settings: Settings) -> None:
    """No gateway headers: 401, never the use case."""
    client, execute = _client(settings, "get_artefact", ErrorCode.ARTEFACT_NOT_FOUND)
    assert client.get(BASE).status_code == 401
    execute.assert_not_awaited()
