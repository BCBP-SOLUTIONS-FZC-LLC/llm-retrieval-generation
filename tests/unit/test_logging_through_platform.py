"""Every log line goes out in the platform's layout, with the request context.

Our modules log with ``fastapicommon.logging.get_logger``; third-party libraries (httpx, asyncpg,
boto3) log through the standard library, and the platform's root handler renders those records
the same way. Both carry ``request_id``, ``correlation_id``, ``tenant_id`` and ``user_id`` inside a
request.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from typing import Any
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from fastapicommon.logging import get_logger

from rag.infrastructure.config import Settings
from rag.presentation.main import create_app


def _json_lines(text: str) -> list[dict[str, Any]]:
    out = []
    for line in text.splitlines():
        try:
            out.append(json.loads(line))
        except ValueError:
            continue
    return out


def _log_in_a_request(
    emit: Callable[[], None], capfd: pytest.CaptureFixture[str]
) -> tuple[Any, ...]:
    app = create_app(Settings(environment="test", enable_tracing=False, enable_metrics=False))

    @app.get("/__probe")
    async def probe() -> dict[str, bool]:
        emit()
        return {"ok": True}

    tenant, user = uuid4(), uuid4()
    response = TestClient(app).get(
        "/__probe", headers={"x-tenant-id": str(tenant), "x-user-id": str(user)}
    )
    assert response.status_code == 200
    return _json_lines(capfd.readouterr().err), tenant, user


def test_our_get_logger_calls_carry_fields_and_the_request_context(
    capfd: pytest.CaptureFixture[str],
) -> None:
    log = get_logger("rag.application.probe")
    lines, tenant, user = _log_in_a_request(
        lambda: log.warning("probe_event", sweep="outbox", count=3), capfd
    )
    entry = next(e for e in lines if e.get("msg") == "probe_event")
    assert {"level", "ts", "caller", "request_id", "correlation_id"} <= entry.keys()
    assert entry["level"] == "warn" and entry["sweep"] == "outbox" and entry["count"] == 3
    assert entry["tenant_id"] == str(tenant) and entry["user_id"] == str(user)


def test_a_third_party_stdlib_logger_is_rendered_by_the_same_handler(
    capfd: pytest.CaptureFixture[str],
) -> None:
    lines, tenant, _ = _log_in_a_request(
        lambda: logging.getLogger("httpx").warning("third party %s", "line"), capfd
    )
    entry = next(e for e in lines if e.get("msg") == "third party line")
    assert {"level", "ts", "caller", "request_id"} <= entry.keys()
    assert entry["tenant_id"] == str(tenant)
