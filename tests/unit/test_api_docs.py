"""The committed API specs match the code, and the docs routes are served."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from rag.infrastructure.config import Settings
from rag.presentation import api_responses
from rag.presentation.main import create_app

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "gen_api_docs.py"


@pytest.fixture(scope="module")
def gen():  # type: ignore[no-untyped-def]
    """The generator script as a module."""
    spec = importlib.util.spec_from_file_location("gen_api_docs", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules["gen_api_docs"] = module
    spec.loader.exec_module(module)
    return module


def test_committed_specs_are_not_stale(gen) -> None:  # type: ignore[no-untyped-def]
    """Run `make swag` after changing a route, a model or an event payload."""
    assert gen.SWAGGER_JSON.read_text() == gen.swagger_json_text(), "run: make swag"
    assert gen.SWAGGER_YAML.read_text() == gen.swagger_yaml_text(), "run: make swag"
    assert gen.ASYNCAPI.read_text() == gen.asyncapi_text(), "run: make swag"
    assert gen.ASYNCAPI_HTML.read_text() == gen.asyncapi_html(), "run: make swag"


def test_asyncapi_covers_every_published_and_consumed_type(gen) -> None:  # type: ignore[no-untyped-def]
    """EVT-9: nothing is published or consumed that the spec does not describe."""
    import yaml

    from rag.application.events.contracts import CONSUMED, PUBLISHED

    doc = yaml.safe_load(gen.ASYNCAPI.read_text())
    assert doc["asyncapi"] == "3.0.0"
    assert set(doc["components"]["messages"]) == {*PUBLISHED, *CONSUMED}
    sent = {
        m
        for c in ("llm-generation-events", "llm-index-events")
        for m in doc["channels"][c]["messages"]
    }
    assert sent == set(PUBLISHED)
    assert "$defs" not in gen.ASYNCAPI.read_text()


def test_docs_routes_are_served() -> None:
    """/docs, /openapi.json, /asyncapi and /asyncapi.yaml answer without identity headers."""
    client = TestClient(create_app(Settings(environment="test", enable_tracing=False)))
    assert client.get("/docs").status_code == 200
    assert "/swagger" in client.get("/").text
    assert "swagger-initializer.js" in client.get("/swagger").text
    assert "SwaggerUIBundle" in client.get("/swagger/swagger-initializer.js").text
    assert "#8426b0" in client.get("/swagger/index.css").text  # the BCBP theme
    assert (
        client.get("/swagger/doc.json").json()["paths"]
        == client.get("/openapi.json").json()["paths"]
    )
    assert "openapi:" in client.get("/swagger/doc.yaml").text
    assert client.get("/openapi.json").json()["paths"]
    page = client.get("/asyncapi").text
    assert "AsyncAPI 3.0.0" in page and "#8426b0" in page and "{{ENV}}" not in page
    assert "GenerationCompleted" in page and "RECEIVE" in page
    spec = client.get("/asyncapi.yaml")
    assert spec.status_code == 200 and spec.text.startswith("asyncapi: 3.0.0")
    assert "/asyncapi" not in client.get("/openapi.json").text


def test_every_operation_declares_its_standard_errors_and_no_phantom_422(gen) -> None:  # type: ignore[no-untyped-def]
    """The default spec said 200 and 422 only: the app answers 400, 401, 500 and 503 as problems."""
    doc = gen.openapi_doc()
    explicit_422 = {k for k, v in api_responses.EXTRA.items() if "422" in v}
    for path, item in doc["paths"].items():
        for method, op in item.items():
            declared = set(op["responses"])
            assert {"500", "503"} <= declared, (method, path)
            if (method, path) not in api_responses.NO_AUTH:
                assert {"401"} <= declared, (method, path)
            assert ("422" in declared) == ((method, path) in explicit_422), (method, path)
    assert "HTTPValidationError" not in doc["components"]["schemas"]
    assert doc["components"]["schemas"]["Problem"]["required"] == ["type", "title", "status"]
    missing = [k for k in api_responses.EXTRA if k[0] not in doc["paths"].get(k[1], {})]
    assert not missing, f"EXTRA names routes that do not exist: {missing}"


def test_a_401_the_app_really_answers_is_declared_as_a_problem(gen) -> None:  # type: ignore[no-untyped-def]
    """The status and media type of a real answer match what the spec now says (400s are covered by
    the API tests, which run a harness; here no database is needed)."""
    app = create_app(
        Settings(
            environment="test", enable_tracing=False, enable_metrics=False, events_enabled=False
        )
    )
    op = gen.openapi_doc()["paths"]["/api/v1/collections"]["get"]
    answer = TestClient(app).get("/api/v1/collections")  # no lifespan, no identity
    assert str(answer.status_code) in op["responses"] and answer.status_code == 401
    assert answer.headers["content-type"].startswith("application/problem+json")
    declared = op["responses"]["401"]["content"]
    assert answer.headers["content-type"].split(";")[0] in declared
    schema = gen.openapi_doc()["components"]["schemas"]["Problem"]["properties"]
    assert set(answer.json()) <= set(schema)  # every member the app wrote is in the schema
