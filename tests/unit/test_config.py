"""Settings: LLD §17 names and defaults, and the PgBouncer startup assertion (ISO-11)."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from rag.infrastructure.config import ConfigError, Settings, assert_isolation_config
from rag.presentation.main import create_app

# LLD §17, variable -> default. REFINEMENT_RETENTION_DAYS is deliberately unset.
LLD_17: dict[str, object] = {
    "SCOPE_TOKEN_TTL_SECONDS": 600,
    "SCOPE_CACHE_TTL_SECONDS": 60,
    "GRANT_READ_TIMEOUT_MS": 100,
    "REFINER_QUERY_DEADLINE_MS": 150,
    "REFINER_INSTRUCTION_DEADLINE_MS": 3000,
    "RERANK_DEADLINE_MS": 90,
    "RERANK_MAX_IN": 20,
    "RERANK_MAX_SEQ_TOKENS": 256,
    "RRF_K": 60,
    "CONTEXT_OUTPUT_RESERVE_FRACTION": 0.25,
    "CONTEXT_USER_INPUT_MAX_FRACTION": 0.10,
    "PROVENANCE_EXCERPT_CHARS": 240,
    "COMPLETENESS_PARTIAL_THRESHOLD": 0.70,
    "FANOUT_MAX_PER_DOCUMENT": 5,
    "ACTIVITY_START_TO_CLOSE_SECONDS": 600,
    "ACTIVITY_HEARTBEAT_SECONDS": 30,
    "ACTIVITY_MAX_ATTEMPTS": 3,
    "FANOUT_MAX_PER_TENANT": 10,
    "FANOUT_MAX_GLOBAL": 100,
    "REVIEW_GATE_DAYS": 30,
    "CONFLICT_REPORT_MAX": 50,
    "REFINEMENT_RETENTION_DAYS": None,
    "REQUEST_TIMEOUT_SECONDS": 120,
    "DR_MODE": False,
    "PROMOTION_TIMEOUT_SECONDS": 86400,
    "TENANT_PURGE_SOAK_DAYS": 7,
    "COLLECTION_NAMESPACE": "8d9d8e69-7546-4011-bf89-15ef4c822386",
    "ORPHAN_PENDING_SECONDS": 300,
    "RETRIEVAL_SET_ORPHAN_MIN_AGE_DAYS": 30,
    "STALLED_GENERATING_SECONDS": 900,
    "GROUNDEDNESS_PARTIAL_THRESHOLD": 0.70,
    "GROUNDEDNESS_SUPPORTED_THRESHOLD": 0.95,
    "IDEMPOTENCY_TTL_SECONDS": 3600,
    "SSE_HEARTBEAT_SECONDS": 15,
    "PAGE_LIMIT_DEFAULT": 100,
    "PAGE_LIMIT_MAX": 500,
    "EXTRACTION_SYNC_TIMEOUT_SECONDS": 90,
    "REPLICA_LAG_WINDOW_SECONDS": 30,
}


@pytest.mark.parametrize(("name", "default"), LLD_17.items())
def test_lld_17_default(name: str, default: object) -> None:
    """Every §17 variable exists on Settings with the documented default."""
    assert getattr(Settings(), name.lower()) == default


def test_env_overrides_by_upper_case_name(monkeypatch: pytest.MonkeyPatch) -> None:
    """The environment variable is the upper-cased field name."""
    monkeypatch.setenv("SCOPE_TOKEN_TTL_SECONDS", "42")
    monkeypatch.setenv("DR_MODE", "true")
    s = Settings()
    assert (s.scope_token_ttl_seconds, s.dr_mode) == (42, True)


def test_problem_type_base_and_metrics_default() -> None:
    """Problem URIs live under errors.bcbp.io (§24); metrics are off until `llm` is registered."""
    s = Settings()
    assert (s.problem_type_base, s.enable_metrics, s.observability_domain) == (
        "https://errors.bcbp.io",
        False,
        "",
    )


def test_migration_dsn_falls_back_to_application_dsn() -> None:
    """A DDL DSN is used when set, else the application DSN."""
    s = Settings(corpus_migration_database_url="postgresql://ddl@h/llm_corpus")
    assert s.migration_dsn("corpus") == "postgresql://ddl@h/llm_corpus"
    assert s.migration_dsn("generation") == s.generation_database_url


def test_pgbouncer_mode_true_passes() -> None:
    """Exactly ``true`` is accepted (and is the default)."""
    assert_isolation_config(Settings())


@pytest.mark.parametrize("value", ["True", "TRUE", "yes", "1", "false", ""])
def test_pgbouncer_mode_anything_else_refuses_to_start(value: str) -> None:
    """Any other value is a tenant-isolation hazard: startup fails (ISO-11)."""
    with pytest.raises(ConfigError, match="PG_BOUNCER_MODE"):
        assert_isolation_config(Settings(pg_bouncer_mode=value))


def test_create_app_refuses_bad_pgbouncer_mode(settings: Settings) -> None:
    """The assertion runs in the app factory, before any pool exists."""
    with pytest.raises(ConfigError):
        create_app(settings.model_copy(update={"pg_bouncer_mode": "True"}))


# --- startup validation: a bad value stops the process, it does not surface later ------------


@pytest.mark.parametrize(
    "overrides",
    [
        {"rrf_k": -5},
        {"rrf_k": 0},
        {"page_limit_max": 0},
        {"scope_token_ttl_seconds": -1},
        {"candidate_overfetch": 0.5},
        {"context_output_reserve_fraction": 1.0},
        {"groundedness_supported_threshold": 1.5},
        {"verify_tier0_sample_rate": -0.1},
        {"collection_namespace": "not-a-uuid"},
        {"corpus_database_url": "garbage"},
        {"generation_database_url": ""},
        {"corpus_sweep_database_url": "mysql://x"},
        {"page_limit_default": 600},  # above the default maximum of 500
        {"request_timeout_seconds": 60.0},  # must exceed R-4's 90 s synchronous budget (§17)
        {"context_output_reserve_fraction": 0.95, "context_user_input_max_fraction": 0.10},
        {"verify_entail_unsupported": 0.9},  # above verify_entail_supported
        {"groundedness_partial_threshold": 0.99},  # above the supported threshold
        {"events_enabled": True},  # no topic ARNs
    ],
)
def test_a_misconfigured_setting_is_rejected_at_construction(overrides: dict[str, object]) -> None:
    """Each of these used to be accepted and would have failed later, or silently misbehaved."""
    with pytest.raises(ValidationError):
        Settings(**overrides)


def test_a_bad_environment_value_stops_startup(monkeypatch: pytest.MonkeyPatch) -> None:
    """The service reads its settings in ``create_app``: a bad variable fails before any pool."""
    monkeypatch.setenv("RRF_K", "-5")
    with pytest.raises(ValidationError, match="rrf_k"):
        create_app()


def test_valid_optional_values_are_still_accepted() -> None:
    """Optional DSNs may be empty; events may be enabled once the ARNs are set."""
    s = Settings(
        corpus_migration_database_url="",
        events_enabled=True,
        generation_topic_arn="arn:aws:sns:eu-west-1:000000000000:g",
        index_topic_arn="arn:aws:sns:eu-west-1:000000000000:i",
        refinement_retention_days=30,
    )
    assert s.events_enabled and s.refinement_retention_days == 30
