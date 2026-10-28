"""Every pool's ``pgcommon.Config`` comes from one factory, with pgcommon's own tuning parsing.

``PG_*`` tuning (timeouts, lifetimes, slow-query threshold) is read and validated by
``pgcommon.config_from_env``; the DSN, pool name and size, PgBouncer mode and the tenant GUC
provider are decisions of this service and always win over the environment.
"""

from __future__ import annotations

import os
from datetime import timedelta

import pytest
from tests.support_logs import RecordingLog

from rag.infrastructure.config import Settings
from rag.infrastructure.persistence import pools
from rag.infrastructure.persistence.pools import pool_config, request_guc_provider

DSN = "postgresql://rag_app_login:pw@pgbouncer:6432/llm_corpus"


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> RecordingLog:
    """No ambient ``PG_*`` / ``DATABASE_URL``, a fresh once-per-process warning memory."""
    for key in [k for k in os.environ if k.startswith("PG") or k.endswith("DATABASE_URL")]:
        monkeypatch.delenv(key)
    monkeypatch.setattr(pools, "_warned", set())
    rec = RecordingLog()
    monkeypatch.setattr(pools, "log", rec)
    return rec


def test_the_defaults_for_an_application_pool(_clean_env: RecordingLog) -> None:
    cfg = pool_config(DSN, "corpus", Settings())
    assert cfg.dsn == DSN and cfg.pool_name == "corpus"
    assert cfg.max_conns == Settings().pg_max_conns
    assert cfg.pgbouncer_mode is True and cfg.guc_provider is request_guc_provider
    assert cfg.min_conns is None  # the library's default for PgBouncer mode (0)
    assert cfg.slow_query_threshold == timedelta(milliseconds=200)  # the library's default
    assert _clean_env.records == []  # nothing noisy about the DSN assembly we do not use


def test_pgcommon_tuning_variables_are_honoured(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PG_STATEMENT_TIMEOUT", "5s")
    monkeypatch.setenv("PG_LOCK_TIMEOUT", "2s")
    monkeypatch.setenv("PG_ACQUIRE_TIMEOUT", "7s")
    monkeypatch.setenv("PG_SLOW_QUERY_THRESHOLD", "900ms")
    monkeypatch.setenv("PG_MAX_CONN_LIFETIME", "2h")
    cfg = pool_config(DSN, "corpus", Settings())
    assert cfg.statement_timeout == timedelta(seconds=5)
    assert cfg.lock_timeout == timedelta(seconds=2)
    assert cfg.acquire_timeout == timedelta(seconds=7)
    assert cfg.slow_query_threshold == timedelta(milliseconds=900)
    assert cfg.max_conn_lifetime == timedelta(hours=2)


def test_what_is_ours_wins_over_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """DSN, name, size, PgBouncer mode and the GUC provider are not tuning."""
    monkeypatch.setenv("DATABASE_URL", "postgresql://someone@elsewhere/other")
    monkeypatch.setenv("PG_POOL_NAME", "from-env")
    monkeypatch.setenv("PG_MAX_CONNS", "99")
    monkeypatch.setenv("PG_BOUNCER_MODE", "false")
    cfg = pool_config(DSN, "generation", Settings(pg_max_conns=10))
    assert (cfg.dsn, cfg.pool_name, cfg.max_conns) == (DSN, "generation", 10)
    assert cfg.pgbouncer_mode is True and cfg.guc_provider is request_guc_provider


def test_min_conns_follows_pg_min_conns_only_when_it_is_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert pool_config(DSN, "c", Settings()).min_conns is None
    monkeypatch.setenv("PG_MIN_CONNS", "3")
    assert pool_config(DSN, "c", Settings()).min_conns == 3
    assert pool_config(DSN, "c", Settings(), min_conns=1).min_conns == 1  # explicit wins


def test_the_migration_and_sweep_variants() -> None:
    migrate = pool_config(
        DSN, "migrate", Settings(), max_conns=1, min_conns=1, guc=False, pgbouncer=False
    )
    assert (migrate.max_conns, migrate.min_conns) == (1, 1)
    assert migrate.guc_provider is None and migrate.pgbouncer_mode is False
    sweep = pool_config(DSN, "sweep", Settings(), max_conns=2, guc=False)
    assert sweep.max_conns == 2 and sweep.guc_provider is None and sweep.pgbouncer_mode is True


def test_a_bad_tuning_value_becomes_a_default_and_one_logged_warning(
    monkeypatch: pytest.MonkeyPatch, _clean_env: RecordingLog
) -> None:
    """The library substitutes a safe default; it asks for the warning to be logged at startup."""
    monkeypatch.setenv("PG_STATEMENT_TIMEOUT", "banana")
    for _ in range(3):  # several pools, one process: warned once
        cfg = pool_config(DSN, "corpus", Settings())
    assert cfg.statement_timeout is None
    [warning] = _clean_env.find("pgcommon_config_warning", "warning")
    assert warning["key"] == "PG_STATEMENT_TIMEOUT" and warning["reason"]
