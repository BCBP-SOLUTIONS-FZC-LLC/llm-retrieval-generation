"""Migration wiring for both instances (§4.11, §18.1). Run from the init container.

Each instance has its own numbered set under ``migrations/<instance>/`` and its own ledger
(the databases are separate). ``eventcommon.outbox.apply_schema`` adds the outbox under its own
``outbox_migrations`` ledger on each database, and ``eventcommon.inbox.apply_schema`` adds the
consumers' dedup ledger (``processed_events``, ``inbox_migrations``) on ``llm_generation``. Both go
*before* the numbered sets so that their grants on those tables find them (the schema calls take
no advisory lock, so they must not race: one job, not every replica). The tables belong to the
library; this repo only grants on them.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from eventcommon.inbox import apply_schema as apply_inbox_schema
from eventcommon.outbox import apply_schema
from pgcommon import Pool, StructlogLogger
from pgcommon.migrate import Runner

from rag.infrastructure.config import Settings
from rag.infrastructure.persistence.pools import pool_config

INSTANCES = ("corpus", "generation")
LEDGER_INSTANCE = "generation"
"""The database holding ``processed_events`` (PE-1): the consumers' ledger rows commit there."""
_REPO_MIGRATIONS = Path(__file__).resolve().parents[4] / "migrations"


def migrations_path(settings: Settings, instance: str) -> Path:
    """Directory holding ``instance``'s ``NNNN_name.up.sql`` / ``.down.sql`` files."""
    root = Path(settings.migrations_dir) if settings.migrations_dir else _REPO_MIGRATIONS
    return root / instance


async def migrate_instance(settings: Settings, instance: str) -> None:
    """Apply every pending migration of one instance with ``pgcommon.migrate.Runner``."""
    runner = Runner(
        settings.migration_dsn(instance),
        migrations_path(settings, instance),
        logger=StructlogLogger(),
    )
    await runner.up()


async def apply_library_schemas(pool: Pool, instance: str) -> None:
    """Eventcommon's tables for ``instance``: the outbox, and the inbox where the ledger lives.

    The one place that knows which library schema goes where; ``migrate_all`` calls it, and so do
    the repository tests' fixtures, so they cannot drift from production's order.
    """
    await apply_schema(pool)
    if instance == LEDGER_INSTANCE:
        await apply_inbox_schema(pool)


async def _apply_library_schemas(settings: Settings, instance: str) -> None:
    """Open a direct, single-connection pool on ``instance`` and apply the library's schemas."""
    pool = await Pool.create(
        # direct to Postgres (the library's advisory lock is session-scoped): no PgBouncer, no GUC
        pool_config(
            settings.migration_dsn(instance),
            "migrate",
            settings,
            max_conns=1,
            min_conns=1,
            guc=False,
            pgbouncer=False,
        )
    )
    try:
        await apply_library_schemas(pool, instance)
    finally:
        await pool.drain_and_close()


async def migrate_all(settings: Settings) -> None:
    """Apply eventcommon's schemas, then both numbered sets (the order matters, see below).

    ``llm_corpus`` carries its own outbox: C-I1, C-I4, C-I8 and the lifecycle consumers write
    their event row in the ``llm_corpus`` transaction that changes the state (EVT-8, OBX-1),
    and a transaction cannot span the two databases (LLD deviation, see the plan). The outbox
    goes **first**, with the inbox: ``0003`` grants ``rag_app`` / ``rag_activity`` on
    ``outbox_events`` and ``rag_app`` on ``processed_events``, which must exist by then (the
    library's tables belong to the migration role, not ``rag_owner``).
    """
    for instance in INSTANCES:
        await _apply_library_schemas(settings, instance)
    for instance in INSTANCES:
        await migrate_instance(settings, instance)


if __name__ == "__main__":
    asyncio.run(migrate_all(Settings()))
