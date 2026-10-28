"""Every ``.down.sql`` runs: up, all the way down, and up again leaves a working schema.

The down files were only ever parsed (and that parse test needs ``pglast``, which is not a
dependency). Here each instance is migrated up, rolled back completely, and migrated up again;
a down that does not reverse its up fails the second ``up`` or leaves tables behind.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import asyncpg
import pytest
from pgcommon import Config, Pool
from pgcommon.migrate import Runner
from tests.repository.test_corpus_repositories import dsn_base  # noqa: F401  (fixture)

from rag.infrastructure.persistence.migrate import apply_library_schemas

pytestmark = pytest.mark.integration

MIGRATIONS = Path(__file__).resolve().parents[2] / "migrations"
APP_TABLES = {"corpus": 5, "generation": 18}  # excluding eventcommon's tables
LIBRARY_TABLES = {
    "outbox_events",
    "outbox_dead_letters",
    "outbox_migrations",
    "processed_events",
    "inbox_migrations",
    "pgcommon_migrations",
}


def _count(instance: str) -> int:
    return len(sorted((MIGRATIONS / instance).glob("*.up.sql")))


@pytest.fixture(params=["corpus", "generation"])
async def instance(request: pytest.FixtureRequest, dsn_base: str) -> AsyncIterator[tuple[str, str]]:  # noqa: F811
    name: str = request.param
    admin = await asyncpg.connect(f"{dsn_base}/postgres")
    db = f"llm_roundtrip_{name}"
    await admin.execute(f"DROP DATABASE IF EXISTS {db}")
    await admin.execute(f"CREATE DATABASE {db}")
    await admin.close()
    url = f"{dsn_base}/{db}"
    pool = await Pool.create(Config(dsn=url, max_conns=1, min_conns=1))
    try:
        await apply_library_schemas(pool, name)  # as migrate_all: 0003 grants on them
    finally:
        await pool.drain_and_close()
    yield name, url


async def _tables(url: str) -> set[str]:
    conn = await asyncpg.connect(url)
    try:
        rows = await conn.fetch("SELECT tablename FROM pg_tables WHERE schemaname = 'public'")
    finally:
        await conn.close()
    return {r["tablename"] for r in rows} - LIBRARY_TABLES


async def test_up_then_all_the_way_down_then_up_again(instance: tuple[str, str]) -> None:
    name, url = instance
    runner = Runner(url, MIGRATIONS / name)
    await runner.up()
    assert len(await _tables(url)) == APP_TABLES[name]

    await Runner(url, MIGRATIONS / name).down(_count(name))
    assert await _tables(url) == set(), "a down migration left tables behind"

    await Runner(url, MIGRATIONS / name).up()
    assert len(await _tables(url)) == APP_TABLES[name]
