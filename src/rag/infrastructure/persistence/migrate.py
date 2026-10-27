"""Migration wiring for both instances (§4.11, §18.1). Run from the init container.

Each instance has its own numbered set under ``migrations/<instance>/`` and its own ledger
(the databases are separate). ``eventcommon.outbox.apply_schema`` adds the outbox under its own
``outbox_migrations`` ledger on each database, *before* the numbered sets so that their grants on
``outbox_events`` find the table (it takes no advisory lock, so it must not race: one job, not
every replica).
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from eventcommon.outbox import apply_schema
from pgcommon import Config, Pool, StructlogLogger
from pgcommon.migrate import Runner

from rag.infrastructure.config import Settings

INSTANCES = ("corpus", "generation")
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


async def _apply_outbox(settings: Settings, instance: str) -> None:
    pool = await Pool.create(
        Config(
            dsn=settings.migration_dsn(instance),
            max_conns=1,
            min_conns=1,
            pool_name="migrate",
            logger=StructlogLogger(),
        )
    )
    try:
        await apply_schema(pool)
    finally:
        await pool.drain_and_close()


async def migrate_all(settings: Settings) -> None:
    """Apply the outbox schema, then both numbered sets (the order matters, see below).

    ``llm_corpus`` carries its own outbox: C-I1, C-I4, C-I8 and the lifecycle consumers write
    their event row in the ``llm_corpus`` transaction that changes the state (EVT-8, OBX-1),
    and a transaction cannot span the two databases (LLD deviation, see the plan). The outbox
    goes **first**: ``0003`` grants ``rag_app`` / ``rag_activity`` on ``outbox_events``, which must
    exist by then (the library's tables belong to the migration role, not ``rag_owner``).
    """
    for instance in INSTANCES:
        await _apply_outbox(settings, instance)
    for instance in INSTANCES:
        await migrate_instance(settings, instance)


if __name__ == "__main__":
    asyncio.run(migrate_all(Settings()))
