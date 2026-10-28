"""The dedup ledger is eventcommon's inbox: the app role claims, dedups and prunes through it.

``processed_events`` is created by ``eventcommon.inbox.apply_schema`` (not by our migrations) and
owned by the migration role, so ``0003`` grants on it explicitly. This exercises those grants as
the real ``rag_app`` login, through the library's own ``Store``, never raw SQL.
"""

from __future__ import annotations

import asyncio
from datetime import timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest
from eventcommon import new_envelope
from eventcommon.inbox import Store
from tests.repository.support import Db

pytestmark = pytest.mark.integration


def _envelope() -> Any:
    return new_envelope("TenderRestricted", "llm-test", {"x": 1}, tenant_id=str(uuid4()))


async def test_a_redelivery_runs_the_effect_once(db: Db) -> None:
    """PE-1: the claim and the effect commit together; a duplicate delivery runs nothing."""
    store, env, effects = Store(db.app, "inbox_probe"), _envelope(), []

    async def effect(conn: Any) -> None:
        effects.append(await conn.fetchval("SELECT 1"))

    await store.process(env, effect)
    await store.process(env, effect)
    assert effects == [1]
    assert await store.is_processed(UUID(env.id))


async def test_consumers_dedup_independently(db: Db) -> None:
    """One table, one row per (event, consumer): another consumer still handles the event."""
    env, seen = _envelope(), []

    async def effect(conn: Any) -> None:
        seen.append(1)

    await Store(db.app, "inbox_a").process(env, effect)
    await Store(db.app, "inbox_b").process(env, effect)
    assert seen == [1, 1]


async def test_a_failed_effect_is_not_recorded_so_the_retry_runs(db: Db) -> None:
    store, env, attempts = Store(db.app, "inbox_retry"), _envelope(), []

    async def flaky(conn: Any) -> None:
        attempts.append(1)
        if len(attempts) == 1:
            raise RuntimeError("transient")

    with pytest.raises(RuntimeError):
        await store.process(env, flaky)
    await store.process(env, flaky)
    assert len(attempts) == 2


async def test_the_app_role_can_prune_old_rows(db: Db) -> None:
    """EVT-16: the sweep deletes as ``rag_app``, which only 0003's explicit grant allows."""
    store = Store(db.app, "inbox_prune")

    async def effect(conn: Any) -> None:
        return None

    for _ in range(3):
        await store.process(_envelope(), effect)
    await asyncio.sleep(0.01)
    assert await store.prune(timedelta(milliseconds=1)) == 3
    assert await store.prune(timedelta(days=1)) == 0
