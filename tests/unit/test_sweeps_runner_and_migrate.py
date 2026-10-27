"""Sweep runner and CLI, migration wiring: argument handling, wiring and ordering, no database."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from rag.application.sweeps import sweeps as s
from rag.infrastructure.config import Settings
from rag.infrastructure.persistence import migrate
from rag.infrastructure.sweeps import __main__ as cli
from rag.infrastructure.sweeps import runner

SWEEP_DSNS = {
    "corpus_sweep_database_url": "postgresql://sweeper@h/llm_corpus",
    "generation_sweep_database_url": "postgresql://sweeper@h/llm_generation",
}


# ---- runner -----------------------------------------------------------------------------


async def test_open_pools_requires_the_enumeration_dsns() -> None:
    """An empty tenant list would look healthy, so a missing DSN refuses to start."""
    with pytest.raises(RuntimeError, match="SWEEP_DATABASE_URL"):
        await runner.open_pools(Settings())
    with pytest.raises(RuntimeError, match="at least 2"):
        await runner.open_pools(Settings(sweep_pool_max_conns=1, **SWEEP_DSNS))


async def test_open_pools_builds_four_pools(monkeypatch: pytest.MonkeyPatch) -> None:
    """Work pools carry the tenant GUC provider, enumeration pools do not."""
    configs: list[Any] = []

    async def create(config: Any) -> Any:
        configs.append(config)
        return MagicMock(drain_and_close=AsyncMock())

    monkeypatch.setattr(runner.Pool, "create", create)
    pools = await runner.open_pools(Settings(**SWEEP_DSNS))
    assert [c.pool_name for c in configs] == [
        "sweep-corpus",
        "sweep-generation",
        "sweep-corpus-enum",
        "sweep-generation-enum",
    ]
    assert [c.guc_provider is not None for c in configs] == [True, True, False, False]
    await pools.close()
    assert pools.corpus.drain_and_close.await_count == 1


def test_lock_key_is_stable_signed_64_bit_and_distinct() -> None:
    """One advisory-lock key per sweep name."""
    keys = {n: runner.lock_key(n) for n in runner.NAMES}
    assert len(set(keys.values())) == len(runner.NAMES)
    assert all(-(2**63) <= k < 2**63 for k in keys.values())
    assert keys == {n: runner.lock_key(n) for n in runner.NAMES}


@pytest.mark.parametrize("name", runner.NAMES)
def test_build_sweep_wires_every_named_sweep(name: str) -> None:
    """Each name builds its use case over the sweep pools."""
    pools = runner.Pools(*(MagicMock() for _ in range(4)))
    assert runner.build_sweep(Settings(), pools, name) is not None


async def test_run_exclusive_skips_when_the_lock_is_held(monkeypatch: pytest.MonkeyPatch) -> None:
    """``pg_try_advisory_xact_lock`` false: the sweep is skipped, not run."""
    sweep = MagicMock(run=AsyncMock(return_value=s.SweepOutcome("x")))

    def fake_run_in_tx(locked_to: bool) -> Any:
        async def run_in_tx(pool: Any, fn: Any) -> Any:
            return await fn(SimpleNamespace(fetchval=AsyncMock(return_value=locked_to)))

        return run_in_tx

    monkeypatch.setattr(runner, "run_in_tx", fake_run_in_tx(False))
    outcome = await runner.run_exclusive(MagicMock(), "outbox", sweep)
    assert outcome.skipped and not sweep.run.await_count
    monkeypatch.setattr(runner, "run_in_tx", fake_run_in_tx(True))
    assert (await runner.run_exclusive(MagicMock(), "outbox", sweep)).name == "x"
    sweep.run.assert_awaited_once()


async def test_run_sweep_validates_the_name_and_closes_pools_it_opened(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unknown name raises; own pools are closed, shared ones are left open."""
    with pytest.raises(ValueError, match="unknown sweep"):
        await runner.run_sweep(Settings(), "nope")
    pools = runner.Pools(*(MagicMock(drain_and_close=AsyncMock()) for _ in range(4)))
    monkeypatch.setattr(runner, "open_pools", AsyncMock(return_value=pools))
    monkeypatch.setattr(runner, "build_sweep", lambda *a: MagicMock())
    monkeypatch.setattr(runner, "run_exclusive", AsyncMock(return_value=s.SweepOutcome("outbox")))
    await runner.run_sweep(Settings(), "outbox")
    assert pools.corpus.drain_and_close.await_count == 1
    shared = runner.Pools(*(MagicMock(drain_and_close=AsyncMock()) for _ in range(4)))
    await runner.run_sweep(Settings(), "audit-chain", shared)
    assert shared.corpus.drain_and_close.await_count == 0


# ---- CLI --------------------------------------------------------------------------------


@pytest.mark.parametrize("argv", [[], ["bogus"], ["outbox", "extra"]])
async def test_cli_usage_exit_code(argv: list[str]) -> None:
    """Wrong arguments: 64, nothing opened."""
    assert await cli.main(argv) == 64


@pytest.mark.parametrize(
    ("outcome", "code"),
    [
        (s.SweepOutcome("x"), 0),
        (s.SweepOutcome("x", skipped="locked"), 0),
        (s.SweepOutcome("x", failed_tenants=1), 1),
        (s.SweepOutcome("x", alerts=["chain break"]), 2),
    ],
)
async def test_cli_exit_codes(
    monkeypatch: pytest.MonkeyPatch, outcome: s.SweepOutcome, code: int
) -> None:
    """0 clean or skipped, 1 a tenant failed, 2 an alert; pools always close."""
    pools = MagicMock(close=AsyncMock())
    monkeypatch.setattr(cli, "open_pools", AsyncMock(return_value=pools))
    monkeypatch.setattr(cli, "run_sweep", AsyncMock(return_value=outcome))
    assert await cli.main(["outbox"]) == code
    pools.close.assert_awaited_once()


async def test_cli_all_runs_every_sweep(monkeypatch: pytest.MonkeyPatch) -> None:
    """``all`` runs each named sweep once."""
    monkeypatch.setattr(cli, "open_pools", AsyncMock(return_value=MagicMock(close=AsyncMock())))
    run = AsyncMock(return_value=s.SweepOutcome("x"))
    monkeypatch.setattr(cli, "run_sweep", run)
    assert await cli.main(["all"]) == 0
    assert run.await_count == len(runner.NAMES)


# ---- migrate ----------------------------------------------------------------------------


def test_migrations_path_defaults_to_the_repo_and_honours_the_override() -> None:
    """``MIGRATIONS_DIR`` empty: ``<repo>/migrations/<instance>``."""
    default = migrate.migrations_path(Settings(), "corpus")
    assert default.parts[-2:] == ("migrations", "corpus") and default.is_dir()
    assert migrate.migrations_path(Settings(migrations_dir="/x"), "generation") == Path(
        "/x/generation"
    )


async def test_migrate_all_applies_the_outbox_before_any_numbered_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """0003 grants on ``outbox_events``, so both outbox schemas go first."""
    order: list[str] = []

    class FakeRunner:
        def __init__(self, dsn: str, path: Path, logger: Any = None) -> None:
            self.dsn = dsn

        async def up(self) -> None:
            order.append(f"up:{self.dsn}")

    pool = MagicMock(drain_and_close=AsyncMock())
    monkeypatch.setattr(migrate, "Runner", FakeRunner)
    monkeypatch.setattr(migrate.Pool, "create", AsyncMock(return_value=pool))
    monkeypatch.setattr(
        migrate, "apply_schema", AsyncMock(side_effect=lambda p: order.append("outbox"))
    )
    settings = Settings(
        corpus_migration_database_url="postgresql://ddl@h/llm_corpus",
        generation_migration_database_url="postgresql://ddl@h/llm_generation",
    )
    await migrate.migrate_all(settings)
    assert order == [
        "outbox",
        "outbox",
        "up:postgresql://ddl@h/llm_corpus",
        "up:postgresql://ddl@h/llm_generation",
    ]
    assert pool.drain_and_close.await_count == 2
