"""E2E harness: the real app (`create_app` + lifespan + real pools) over a real Postgres 18.

One throwaway Postgres container per session, a fresh pair of databases per test, both fully
migrated by the service's own ``migrate_all``. The app's pools log in as a non-superuser
member of ``rag_app``: a superuser bypasses RLS even with ``FORCE``, which would make every
isolation test vacuous. Only the HTTP peers (Tender catalogue, Qdrant counts, scope resolver)
are faked; the database, RLS, outbox and request-context wiring are the production ones.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from typing import Any
from uuid import UUID, uuid4

import asyncpg
import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from rag.infrastructure.config import Settings
from rag.infrastructure.persistence.corpus.transactions import CorpusTransactionRunner
from rag.infrastructure.persistence.migrate import migrate_all
from rag.presentation.corpus_dependencies import build_corpus_services, get_corpus_services
from rag.presentation.main import create_app
from tests.support_corpus import FakeCatalogue, FakePoints, FakeScope

PASSWORD = "e2e-pw"
LOGIN = "rag_e2e_login"
DBS = {"corpus": "e2e_corpus", "generation": "e2e_generation"}

TENANT_A = UUID("0a3f0000-0000-7000-8000-0000000000a1")
TENANT_B = UUID("0a3f0000-0000-7000-8000-0000000000b1")
USER = UUID("0a3f0000-0000-7000-8000-000000000002")
TENDER = UUID("0a3f0000-0000-7000-8000-0000000000d1")


def identity(
    tenant: UUID | None = TENANT_A, *roles: str, user: UUID | None = USER
) -> dict[str, str]:
    """Gateway-injected identity headers; any part may be left out."""
    headers: dict[str, str] = {}
    if tenant is not None:
        headers["x-tenant-id"] = str(tenant)
    if user is not None:
        headers["x-user-id"] = str(user)
    if roles:
        headers["x-tenant-roles"] = ",".join(roles)
    return headers


def internal(tenant: UUID | None = TENANT_A) -> dict[str, str]:
    """Service-to-service call: tenant header only (``require_tenant``)."""
    return {} if tenant is None else {"x-tenant-id": str(tenant)}


@pytest.fixture(scope="session")
def pg_base() -> Iterator[str]:
    """``postgresql://user:pw@host:port`` of a superuser; skips when Docker is unavailable.

    ``RAG_TEST_PG_DSN_BASE`` points the suite at an existing server instead.
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
    host, port = container.get_container_host_ip(), container.get_exposed_port(5432)
    yield f"postgresql://test:test@{host}:{port}"
    container.stop()


async def _provision(base: str) -> dict[str, str]:
    """Fresh databases, migrated as the superuser, plus the non-superuser app login."""
    admin = await asyncpg.connect(f"{base}/postgres")
    for db in DBS.values():
        await admin.execute(f"DROP DATABASE IF EXISTS {db} WITH (FORCE)")
        await admin.execute(f"CREATE DATABASE {db}")
    await admin.close()
    admin_dsns = {k: f"{base}/{db}" for k, db in DBS.items()}
    await migrate_all(
        Settings(
            environment="test",
            corpus_migration_database_url=admin_dsns["corpus"],
            generation_migration_database_url=admin_dsns["generation"],
        )
    )
    for dsn in admin_dsns.values():
        conn = await asyncpg.connect(dsn)
        await conn.execute(
            f"DO $$ BEGIN IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = '{LOGIN}') THEN "
            f"CREATE ROLE {LOGIN} LOGIN PASSWORD '{PASSWORD}' IN ROLE rag_app; END IF; END $$"
        )
        await conn.close()
    return admin_dsns


@dataclass
class Stack:
    """A running app plus handles for assertions below the HTTP surface."""

    client: TestClient
    app: FastAPI
    scope: FakeScope
    admin_dsns: dict[str, str]

    def sql(self, instance: str, query: str, *args: Any) -> list[asyncpg.Record]:
        """Run ``query`` as the superuser (sees every tenant) against ``instance``."""

        async def run() -> list[asyncpg.Record]:
            conn = await asyncpg.connect(self.admin_dsns[instance])
            try:
                return await conn.fetch(query, *args)
            finally:
                await conn.close()

        return asyncio.run(run())


@pytest.fixture
def make_stack(pg_base: str) -> Iterator[Callable[..., Stack]]:
    """Factory so a test can tweak ``Settings``; every stack is closed on teardown."""
    admin_dsns = asyncio.run(_provision(pg_base))
    host = pg_base.split("@", 1)[1]
    opened: list[TestClient] = []

    def build(**overrides: Any) -> Stack:
        settings = Settings(
            environment="test",
            enable_tracing=False,
            enable_metrics=False,
            pg_max_conns=4,
            corpus_database_url=f"postgresql://{LOGIN}:{PASSWORD}@{host}/{DBS['corpus']}",
            generation_database_url=f"postgresql://{LOGIN}:{PASSWORD}@{host}/{DBS['generation']}",
            **overrides,
        )
        app = create_app(settings)
        scope = FakeScope()

        def corpus_services(request: Request) -> Any:
            container = request.app.state.container
            return build_corpus_services(
                settings,
                CorpusTransactionRunner(container.corpus_pool),
                scope=scope,
                points=FakePoints(0),
                catalogue=FakeCatalogue(),
            )

        app.dependency_overrides[get_corpus_services] = corpus_services
        client = TestClient(app, raise_server_exceptions=False)
        client.__enter__()  # runs the lifespan: opens the real pools
        opened.append(client)
        return Stack(client, app, scope, admin_dsns)

    yield build
    for client in opened:
        client.__exit__(None, None, None)


@pytest.fixture
def stack(make_stack: Callable[..., Stack]) -> Stack:
    """The default stack: events off, no workflow engine."""
    return make_stack()


def new_id() -> UUID:
    """A random UUID."""
    return uuid4()
