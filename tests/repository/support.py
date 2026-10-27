"""Shared helpers for repository tests (a plain module so the ContextVar exists once)."""

from __future__ import annotations

import contextlib
from collections.abc import Iterator
from contextvars import ContextVar
from dataclasses import dataclass
from uuid import UUID

import asyncpg
from pgcommon import GUCSet, Pool

from rag.infrastructure.persistence.transactions import PgTransactionRunner

_tenant: ContextVar[UUID | None] = ContextVar("repo_test_tenant", default=None)


def guc() -> GUCSet | None:
    """``pgcommon`` GUC provider: the tenant bound by ``as_tenant`` (None: no GUC at all)."""
    tenant = _tenant.get()
    return None if tenant is None else GUCSet(tenant_id=str(tenant))


@contextlib.contextmanager
def as_tenant(tenant: UUID | None) -> Iterator[None]:
    """Bind the tenant GUC provider for the enclosed awaits."""
    token = _tenant.set(tenant)
    try:
        yield
    finally:
        _tenant.reset(token)


@dataclass
class Db:
    """One migrated ``llm_generation`` database and the ways to reach it."""

    admin_dsn: str
    app: Pool
    activity: Pool
    app_tx: PgTransactionRunner
    activity_tx: PgTransactionRunner

    async def admin(self) -> asyncpg.Connection:
        """A superuser connection (bypasses RLS: for catalog queries and setup only)."""
        return await asyncpg.connect(self.admin_dsn)
