"""Fixtures for repository tests against a real Postgres 18 (testcontainers). Needs Docker.

Everything is exercised as the non-superuser ``rag_app`` / ``rag_activity`` login roles: a
superuser bypasses RLS even with ``FORCE``, which would make the isolation tests vacuous.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import asyncpg
import pytest
from eventcommon.outbox import apply_schema
from pgcommon import Config, Pool
from pgcommon.migrate import Runner
from tests.repository.support import Db, guc

from rag.infrastructure.persistence.transactions import PgTransactionRunner

MIGRATIONS = Path(__file__).resolve().parents[2] / "migrations" / "generation"
DB = "llm_generation_repo"
PASSWORD = "pw"


@pytest.fixture(scope="module")
def dsn_base() -> Iterator[str]:
    """A throwaway Postgres 18; skips the module when Docker is unavailable.

    ``RAG_TEST_PG_DSN_BASE`` (``postgresql://user:pw@host:port``, a superuser) points the tests
    at an existing server instead; it must provide ``uuidv7()`` (PG 18 does).
    """
    external = os.environ.get("RAG_TEST_PG_DSN_BASE")
    if external:
        yield external
        return
    pytest.importorskip("testcontainers.postgres")
    from testcontainers.postgres import PostgresContainer

    try:
        container = PostgresContainer("postgres:18", username="test", password="test")
        container.start()
    except Exception as exc:  # no Docker daemon
        pytest.skip(f"Docker unavailable: {exc}")
    host, port = container.get_container_host_ip(), container.get_exposed_port(5432)
    yield f"postgresql://test:test@{host}:{port}"
    container.stop()


async def _login_pool(dsn_base: str, role: str, name: str) -> Pool:
    host = dsn_base.split("@", 1)[1]
    return await Pool.create(
        Config(
            dsn=f"postgresql://{role}:{PASSWORD}@{host}/{DB}",
            max_conns=6,
            min_conns=1,
            pgbouncer_mode=True,
            guc_provider=guc,
            pool_name=name,
        )
    )


@pytest.fixture
async def db(dsn_base: str) -> AsyncIterator[Db]:
    """A fresh, fully migrated database with login roles in ``rag_app`` / ``rag_activity``."""
    admin = await asyncpg.connect(f"{dsn_base}/postgres")
    await admin.execute(f"DROP DATABASE IF EXISTS {DB} WITH (FORCE)")
    await admin.execute(f"CREATE DATABASE {DB}")
    await admin.close()
    admin_dsn = f"{dsn_base}/{DB}"
    # eventcommon's outbox first: migration 0003 grants on it (as migrate_all orders them)
    outbox = await Pool.create(Config(dsn=admin_dsn, max_conns=1, min_conns=1, pool_name="outbox"))
    await apply_schema(outbox)
    await outbox.drain_and_close()
    await Runner(admin_dsn, MIGRATIONS).up()
    conn = await asyncpg.connect(admin_dsn)
    for login, group in (("rag_app_login", "rag_app"), ("rag_activity_login", "rag_activity")):
        await conn.execute(
            f"DO $$ BEGIN IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = '{login}') THEN "
            f"CREATE ROLE {login} LOGIN PASSWORD '{PASSWORD}' IN ROLE {group}; END IF; END $$"
        )
    await conn.close()
    app = await _login_pool(dsn_base, "rag_app_login", "repo_app")
    activity = await _login_pool(dsn_base, "rag_activity_login", "repo_activity")
    try:
        yield Db(admin_dsn, app, activity, PgTransactionRunner(app), PgTransactionRunner(activity))
    finally:
        await app.drain_and_close()
        await activity.drain_and_close()
