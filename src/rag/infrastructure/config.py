"""Service settings: every LLD §17 variable (exact names and defaults) plus infrastructure.

Environment variable names are the upper-cased field names. ``Settings`` extends
``fastapicommon.config.PlatformSettings`` so the platform fields (``APP_ENV``,
``OBSERVABILITY_DOMAIN``, ``PROBLEM_TYPE_BASE``, ...) come from the one base class.
"""

from __future__ import annotations

from fastapicommon.config import PlatformSettings

COLLECTION_NAMESPACE_DEFAULT = "8d9d8e69-7546-4011-bf89-15ef4c822386"


class ConfigError(RuntimeError):
    """Raised at startup when a setting would silently weaken a control."""


class Settings(PlatformSettings):
    """All configuration of the service. Defaults are LLD §17's design defaults (CFG-1)."""

    # --- platform overrides -------------------------------------------------------------
    app_name: str = "retrieval-generation"
    problem_type_base: str = "https://errors.bcbp.io"
    # §17: must exceed R-4's 90 s synchronous budget.
    request_timeout_seconds: float = 120.0
    # The platform metrics registry has no "llm" domain yet (LLD OQ-103), and create_platform_app
    # raises on any configured domain outside it, even with metrics off. Until it is registered
    # OBSERVABILITY_DOMAIN stays unset and metrics stay off; then set OBSERVABILITY_DOMAIN=llm
    # and ENABLE_METRICS=true (Tier-3 names are llm_retrieval_generation_*, LLD §16).
    enable_metrics: bool = False

    # --- LLD §17 ------------------------------------------------------------------------
    scope_token_ttl_seconds: int = 600
    scope_cache_ttl_seconds: int = 60
    grant_read_timeout_ms: int = 100
    refiner_query_deadline_ms: int = 150
    refiner_instruction_deadline_ms: int = 3000
    rerank_deadline_ms: int = 90
    rerank_max_in: int = 20
    rerank_max_seq_tokens: int = 256
    rrf_k: int = 60
    context_output_reserve_fraction: float = 0.25
    context_user_input_max_fraction: float = 0.10
    provenance_excerpt_chars: int = 240
    completeness_partial_threshold: float = 0.70
    fanout_max_per_document: int = 5
    activity_start_to_close_seconds: int = 600
    activity_heartbeat_seconds: int = 30
    activity_max_attempts: int = 3
    fanout_max_per_tenant: int = 10
    fanout_max_global: int = 100
    review_gate_days: int = 30
    conflict_report_max: int = 50
    refinement_retention_days: int | None = None  # deliberately unset (D-28, OQ-20)
    dr_mode: bool = False
    promotion_timeout_seconds: int = 86400
    tenant_purge_soak_days: int = 7
    collection_namespace: str = COLLECTION_NAMESPACE_DEFAULT
    orphan_pending_seconds: int = 300
    retrieval_set_orphan_min_age_days: int = 30
    stalled_generating_seconds: int = 900
    groundedness_partial_threshold: float = 0.70
    groundedness_supported_threshold: float = 0.95
    idempotency_ttl_seconds: int = 3600
    sse_heartbeat_seconds: int = 15
    page_limit_default: int = 100
    page_limit_max: int = 500
    extraction_sync_timeout_seconds: int = 90
    replica_lag_window_seconds: int = 30

    # --- retrieval slice (design defaults, CFG-1; not LLD §17 variables) ---------------------
    refiner_query_enabled: bool = True  # flag ``llm.refiner.query`` (§10.5); on by default
    refiner_instruction_enabled: bool = True  # flag ``llm.refiner.instruction`` (§10.5)
    embedding_profile: str = "embed-default"
    generation_context_window_tokens: int = 128_000  # assumption: the Gateway catalogue has none
    mean_chunk_tokens: int = 350
    candidate_overfetch: float = 1.5
    # --- generation slice (design defaults, CFG-1; the §8.1 routing figures are "carried in §17")
    verify_span_overlap: float = 0.60  # tier 1 below this routes to tier 2
    verify_entail_supported: float = 0.70  # tier 2 at or above: supported
    verify_entail_unsupported: float = 0.40  # tier 2 below: unsupported; between: tier 3
    verify_tier0_disagreement: float = 0.20  # tier 0 above this routes the claims up
    verify_tier0_sample_rate: float = 0.10  # the share of non-compliance sections diffed
    generation_answer_deadline_seconds: float = 12.0  # Gateway call: 2x the 6 s target (§14.3)
    generation_draft_deadline_seconds: float = 40.0  # 2x the 20 s target
    # Not carried by the resolved scope yet (index_generations holds the chunker version).
    pipeline_parser_version: str = "unrecorded"
    pipeline_chunker_version: str = "unrecorded"
    # Ingestion's lexical table is not in this repo (DEP-5): its name is an assumption.
    lexical_chunks_table: str = "document_chunks"

    # --- Postgres: two instances (§4.1), PgBouncer transaction mode (§18.2) ---------------
    corpus_database_url: str = "postgresql://rag_app@localhost:5432/llm_corpus"
    generation_database_url: str = "postgresql://rag_app@localhost:5432/llm_generation"
    # DDL-privileged role, direct to Postgres, never through PgBouncer (advisory lock is
    # session-scoped). Empty: fall back to the application DSN.
    corpus_migration_database_url: str = ""
    generation_migration_database_url: str = ""
    # Kept a string on purpose: pgcommon parses booleans leniently and a typo silently turns
    # transaction-local GUCs into session-scoped ones (a cross-tenant leak). Exactly "true".
    pg_bouncer_mode: str = "true"
    # Must equal PgBouncer's pool_size for the db/user pair (§18.2).
    pg_max_conns: int = 10
    # LLD §18.1 runs eventcommon.apply_schema from the init container, not every replica.
    outbox_apply_schema_on_startup: bool = False
    # Directory holding corpus/ and generation/ migration sets; empty: the repo's migrations/.
    migrations_dir: str = ""

    # --- other stores and peers ---------------------------------------------------------
    valkey_url: str = "redis://localhost:6379/0"
    qdrant_url: str = "http://localhost:6333"
    qdrant_api_key: str = ""
    s3_bucket: str = "rag-artefacts"
    s3_region: str = "eu-west-1"
    s3_endpoint_url: str = ""
    s3_kms_key_id: str = ""
    # Empty: no engine is wired, the async entry points answer 503 WORKFLOW_ENGINE_UNAVAILABLE and
    # the orphan sweep / quota consumer refuse rather than report (fail closed, like every port).
    temporal_address: str = ""
    temporal_namespace: str = "llm-generation"  # LLD §9.1
    temporal_task_queue: str = "llm-generation"
    # WP-10: dedicated queues for enterprise-tier and strict-residency tenants (names are ours).
    temporal_task_queue_enterprise: str = "llm-generation-enterprise"
    temporal_task_queue_strict: str = "llm-generation-strict"
    gateway_base_url: str = "http://model-gateway.internal"
    tender_base_url: str = "http://tender.internal"
    iam_base_url: str = "http://iam.internal"

    # --- events: relay, consumers, sweeps (§12, §18.3, §21.2) -----------------------------------
    # Off by default so a replica without AWS (tests, local) starts. When true the relays and
    # every consumer whose queue URL is set run in process (EVT-8).
    events_enabled: bool = False
    aws_region: str = "eu-west-1"
    aws_endpoint_url: str = ""  # an emulator (LocalStack) for CI and local runs
    generation_topic_arn: str = ""  # llm-generation-events
    index_topic_arn: str = ""  # llm-index-events
    outbox_poll_interval_seconds: float = 5.0
    outbox_batch_size: int = 50
    outbox_max_attempts: int = 5
    outbox_publish_timeout_seconds: float = 10.0
    document_queue_url: str = ""  # llm-document-rag-q
    domainprofile_queue_url: str = ""  # domainprofile-llm-invalidate-q
    iam_purge_queue_url: str = ""  # iam-rag-purge-q
    iam_membership_queue_url: str = ""  # iam-rag-membership-q
    tender_queue_url: str = ""  # tender-rag-q
    usage_queue_url: str = ""  # usage-rag-q
    sqs_concurrency: int = 1
    sqs_visibility_timeout_seconds: int = 60
    # LD-86: a message dead-letters at the fourth receive. eventcommon forwards a message whose
    # receive count *exceeds* this value, so 3 makes the fourth receive the one that is
    # forwarded; the queue's own redrive policy (five) is the backstop.
    sqs_max_receive_count: int = 3
    sqs_queue_depth_interval_seconds: float = 30.0
    quota_resume_batch_size: int = 10  # LD-70: a value the LLD leaves open
    # This service's own service-account id on system-initiated audit entries (§4.6.3, OQ-108).
    service_account_id: str = "00000000-0000-7000-8000-4c4c4d000010"
    processed_events_retention_days: int = 8
    destructive_ledger_retention_days: int = 90  # EVT-16: longer than any effect horizon
    outbox_prune_older_than_days: int = 7
    graph_checkpoint_max_age_hours: int = 24
    # Sweeps (§18.8): their own small pools, never the request pools. The enumeration DSNs log
    # in as a member of ``rag_sweeper`` (migration 0003): the only role that may list tenants.
    sweep_pool_max_conns: int = 2
    corpus_sweep_database_url: str = ""
    generation_sweep_database_url: str = ""

    def migration_dsn(self, instance: str) -> str:
        """The DDL DSN for ``"corpus"`` or ``"generation"``, else the application DSN."""
        app, migration = {
            "corpus": (self.corpus_database_url, self.corpus_migration_database_url),
            "generation": (self.generation_database_url, self.generation_migration_database_url),
        }[instance]
        return migration or app


def assert_isolation_config(settings: Settings) -> None:
    """Refuse to start unless ``PG_BOUNCER_MODE`` is exactly ``"true"`` (§4.8.1, §18.1, ISO-11).

    With a GUC provider set and ``pgbouncer_mode`` false, pgcommon injects the tenant GUC
    session-scoped and PgBouncer hands that backend to the next client with ``app.tenant_id``
    still bound. The match is exact: ``"True"`` and ``"yes"`` fail.
    """
    if settings.pg_bouncer_mode != "true":
        raise ConfigError(
            f"PG_BOUNCER_MODE must be exactly 'true' (got {settings.pg_bouncer_mode!r}): "
            "any other value leaves tenant GUCs session-scoped behind PgBouncer"
        )
