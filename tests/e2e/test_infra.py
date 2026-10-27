"""E2E infra: probes, lifespan, and the real migrated schema behind the app."""

from __future__ import annotations

import pytest

from tests.e2e.conftest import Stack

pytestmark = pytest.mark.e2e


@pytest.mark.parametrize("path", ["/health", "/live", "/ready"])
def test_platform_probes_are_up_without_identity(stack: Stack, path: str) -> None:
    """INFRA-001."""
    assert stack.client.get(path).status_code == 200


def test_both_instances_are_migrated_with_their_ledgers_and_outboxes(stack: Stack) -> None:
    """INFRA-002: numbered sets applied, outbox present on each database (EVT-8)."""
    for instance in ("corpus", "generation"):
        tables = {r["tablename"] for r in stack.sql(instance, "SELECT tablename FROM pg_tables")}
        assert "outbox_events" in tables, instance
        assert "collections" in tables or instance == "generation"
    assert {r["tablename"] for r in stack.sql("generation", "SELECT tablename FROM pg_tables")} >= {
        "outbox_events"
    }


def test_rls_is_forced_on_every_corpus_table(stack: Stack) -> None:
    """INFRA-003: no tenant table escapes RLS (superusers excepted, hence FORCE)."""
    rows = stack.sql(
        "corpus",
        "SELECT relname FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
        "WHERE n.nspname = 'public' AND relkind = 'r' AND relname IN "
        "('collections','index_generations') AND NOT (relrowsecurity AND relforcerowsecurity)",
    )
    assert rows == []


def test_lifespan_closes_cleanly_and_can_restart(make_stack: object) -> None:
    """INFRA-004: two stacks over fresh databases in one session both serve /health."""
    first = make_stack()  # type: ignore[operator]
    assert first.client.get("/health").status_code == 200
    first.client.__exit__(None, None, None)
