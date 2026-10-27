"""Migrations against a real Postgres 18 (testcontainers). Needs Docker; skipped by default."""

from __future__ import annotations

import os
from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import asyncpg
import pytest
from eventcommon.outbox import apply_schema
from pgcommon import Config, Pool
from pgcommon.migrate import Runner

pytestmark = pytest.mark.integration

MIGRATIONS = Path(__file__).resolve().parents[2] / "migrations"
FUNCTIONS = {
    "reject_update_except",
    "reject_column_change",
    "reject_always",
    "touch_updated_at",
    "bump_record_version",
}


@pytest.fixture(scope="module")
def dsn_base() -> Iterator[str]:
    """A throwaway Postgres 18; skips the module when Docker is unavailable.

    ``RAG_TEST_PG_DSN_BASE`` points the tests at an existing server instead.
    """
    if external := os.environ.get("RAG_TEST_PG_DSN_BASE"):
        yield external
        return
    pytest.importorskip("testcontainers.postgres")
    from testcontainers.postgres import PostgresContainer

    try:
        container = PostgresContainer("postgres:18", username="test", password="test")
        container.start()
    except Exception as exc:  # no Docker daemon
        pytest.skip(f"Docker unavailable: {exc}")
    yield f"postgresql://test:test@{container.get_container_host_ip()}:{container.get_exposed_port(5432)}"
    container.stop()


@pytest.fixture
async def dsns(dsn_base: str) -> AsyncIterator[dict[str, str]]:
    """One database per instance on the shared cluster."""
    admin = await asyncpg.connect(f"{dsn_base}/postgres")
    for db in ("llm_corpus", "llm_generation"):
        await admin.execute(f"DROP DATABASE IF EXISTS {db}")
        await admin.execute(f"CREATE DATABASE {db}")
    await admin.close()
    yield {"corpus": f"{dsn_base}/llm_corpus", "generation": f"{dsn_base}/llm_generation"}


async def _names(dsn: str, query: str) -> set[str]:
    conn = await asyncpg.connect(dsn)
    try:
        return {r[0] for r in await conn.fetch(query)}
    finally:
        await conn.close()


@pytest.mark.parametrize("instance", ["corpus", "generation"])
async def test_up_down_up(dsns: dict[str, str], instance: str) -> None:
    """Both sets apply, roll back cleanly and re-apply; objects are owned by rag_owner."""
    pool = await Pool.create(Config(dsn=dsns[instance], max_conns=1, min_conns=1))
    try:
        await apply_schema(pool)  # 0003 grants on the outbox, so it exists first (migrate_all)
    finally:
        await pool.drain_and_close()
    runner = Runner(dsns[instance], MIGRATIONS / instance)
    await runner.up()
    types = await _names(dsns[instance], "SELECT typname FROM pg_type WHERE typtype = 'e'")
    assert "grant_level" in types
    assert ("evidence_tier" in types) == (instance == "generation")
    funcs = await _names(
        dsns[instance],
        "SELECT proname FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace "
        "WHERE n.nspname = 'public'",
    )
    assert FUNCTIONS <= funcs
    owners = await _names(
        dsns[instance],
        "SELECT pg_get_userbyid(proowner) FROM pg_proc WHERE proname = 'reject_always'",
    )
    assert owners == {"rag_owner"}
    await runner.down(4)
    assert not await _names(dsns[instance], "SELECT typname FROM pg_type WHERE typtype = 'e'")
    await runner.up()


async def test_outbox_schema_applies_on_generation(dsns: dict[str, str]) -> None:
    """eventcommon's outbox lands beside our ledger on llm_generation."""
    pool = await Pool.create(Config(dsn=dsns["generation"], max_conns=1, min_conns=1))
    try:
        await apply_schema(pool)
    finally:
        await pool.drain_and_close()
    await Runner(dsns["generation"], MIGRATIONS / "generation").up()
    tables = await _names(
        dsns["generation"], "SELECT tablename FROM pg_tables WHERE schemaname = 'public'"
    )
    assert {"outbox_events", "pgcommon_migrations", "outbox_migrations"} <= tables
