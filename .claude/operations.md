# Operations

## 10. Security

### 10.1 Tenant isolation

- **Layer 1 — identity:** headers injected by the gateway after authentication; nothing here parses a token (API-1, API-11). Internal routes authenticate by `x-tenant-id` alone and rely on mesh mTLS and NetworkPolicy.
- **Layer 2 — Postgres:** `ENABLE` + `FORCE ROW LEVEL SECURITY` on every table except `processed_events` and the outbox; the application logs in as a non-superuser in `rag_app`; the tenant GUC is bound transaction-locally (RLS-6) and `PG_BOUNCER_MODE` must be exactly `true`.
- **Layer 3 — vector store:** one mandatory compound filter built in `corpusscope`; `is_current` unrelaxable; the only importer of `qdrant_client`.

### 10.2 Other controls

- Absent and ungranted resources are the same `404` (CG-8); an empty scope is an empty answer; a failed scope read is `503`.
- Six-layer prompts with authority and cacheability per layer, a nonce boundary around retrieved content, pack-regex input screening; retrieved text never in a cached prefix.
- S3 keys under `tenants/<tenant>/…` only; SSE-KMS with `tenant_id` in the encryption context.
- No content in logs or metric labels (OBS-2); no provider credential in this service.
- Append-only audit chain verified by a sweep that pages on a break and never self-heals.
- Output content safety is Tender's (§15.3).

## 11. Configuration

Names are the upper-cased `Settings` fields (`src/rag/infrastructure/config.py`); `.env-example` is the local set. LLD §17 variables keep their exact names and defaults (`tests/unit/test_config.py`).

| Group | Variables |
|---|---|
| Databases | `CORPUS_DATABASE_URL`, `GENERATION_DATABASE_URL`, `CORPUS_MIGRATION_DATABASE_URL`, `GENERATION_MIGRATION_DATABASE_URL`, `PG_BOUNCER_MODE`, `PG_MAX_CONNS` (= PgBouncer pool size), `MIGRATIONS_DIR`, `OUTBOX_APPLY_SCHEMA_ON_STARTUP` Optional pool tuning is `pgcommon`'s own `PG_*` set, read from the process environment (`make run` exports `.env`): `PG_STATEMENT_TIMEOUT`, `PG_LOCK_TIMEOUT`, `PG_ACQUIRE_TIMEOUT`, `PG_SLOW_QUERY_THRESHOLD`, `PG_MAX_CONN_LIFETIME`, `PG_MAX_CONN_IDLE_TIME`, `PG_HEALTH_CHECK_PERIOD`, `PG_MIN_CONNS`; a bad value becomes the library's default plus a logged `pgcommon_config_warning`. |
| Sweeps | `CORPUS_SWEEP_DATABASE_URL`, `GENERATION_SWEEP_DATABASE_URL`, `SWEEP_POOL_MAX_CONNS` (≥ 2) |
| Stores | `VALKEY_URL`, `QDRANT_URL`, `QDRANT_API_KEY`, `S3_BUCKET`, `S3_REGION`, `S3_ENDPOINT_URL`, `S3_KMS_KEY_ID` |
| Peers | `GATEWAY_BASE_URL`, `GATEWAY_CHAT_PATH` / `_EMBEDDINGS_PATH` / `_RERANK_PATH` / `_TOKENIZE_PATH` / `_MODELS_PATH` and `GATEWAY_TENANT_HEADER` / `_RESIDENCY_HEADER` / `_PLAN_HEADER` / `_PREFERRED_MODEL_HEADER`, `TENDER_BASE_URL`, `IAM_BASE_URL`, `TEMPORAL_ADDRESS`, `TEMPORAL_NAMESPACE`, `TEMPORAL_TASK_QUEUE`, `…_ENTERPRISE`, `…_STRICT` |
| Events | `EVENTS_ENABLED`, `AWS_REGION`, `AWS_ENDPOINT_URL`, `GENERATION_TOPIC_ARN`, `INDEX_TOPIC_ARN`, `DOCUMENT_QUEUE_URL`, `DOMAINPROFILE_QUEUE_URL`, `IAM_PURGE_QUEUE_URL`, `IAM_MEMBERSHIP_QUEUE_URL`, `TENDER_QUEUE_URL`, `USAGE_QUEUE_URL`, `SQS_MAX_RECEIVE_COUNT` (ours, default 3), `OUTBOX_PRUNE_OLDER_THAN_DAYS`. Tuning is `eventcommon`'s own, read from the process environment: `OUTBOX_POLL_INTERVAL`, `OUTBOX_BATCH_SIZE`, `OUTBOX_MAX_ATTEMPTS`, `OUTBOX_PUBLISH_TIMEOUT`, `OUTBOX_DRAIN_TIMEOUT`, `SQS_CONCURRENCY`, `SQS_MAX_MESSAGES`, `SQS_WAIT_SECONDS`, `SQS_HANDLER_TIMEOUT`, `SQS_VISIBILITY_TIMEOUT` (service default 60s), `SQS_QUEUE_DEPTH_INTERVAL` (service default 30s); durations need a unit (`5s`). |
| Behaviour | `SCOPE_*`, `RRF_K`, `RERANK_*`, `FANOUT_MAX_*`, `IDEMPOTENCY_TTL_SECONDS`, `REQUEST_TIMEOUT_SECONDS` (120, must exceed R-4's 90 s), `DR_MODE`, `REFINER_*_ENABLED`, `VERIFY_*`, … |
| Platform | `APP_ENV`, `APP_NAME`, `OBSERVABILITY_DOMAIN`, `ENABLE_METRICS` (false until the `llm` domain is registered), `ENABLE_TRACING`, `PROBLEM_TYPE_BASE` |

Startup refuses: `PG_BOUNCER_MODE` ≠ `true`; an unreachable database at the initial ping; a metrics domain outside the platform registry. The sweep runner refuses without the two enumeration DSNs.

## 12. Sweeps

Run as `python -m rag.infrastructure.sweeps <name>|all`, intended as CronJobs from the service image (**planned** — no image or chart yet). Exit `0` clean or skipped, `1` a tenant failed, `2` an alert, `64` usage. Ten sweeps: orphan-artefacts, stalled-generations, idempotency-keys, retrieval-sets, graph-checkpoints, outbox, processed-events, audit-chain, tenant-purge, stalled-promotions. Per-instance advisory lock; `DR_MODE` disables the destructive ones.

## 13. Observability

Logs, spans and `llm_retrieval_generation_*` metrics through `fastapicommon`. **Metrics are off by default** (OQ-103). See [`docs/observability/`](../docs/observability/README.md) and the runbooks under `docs/`.

## 14. Deployment

**Planned** — no `Dockerfile`, Helm chart or release workflow yet ([`VERSIONING.md`](../VERSIONING.md#planned-not-yet-in-place)). CI today only validates (`ci.yml`: `validate-test`, `validate-quality`, `pr-summary`; `changelog-check.yml`). Migrations must run from a one-shot job directly against Postgres before the new image takes traffic, and be safe while the previous image serves.

## 15. Local stack

`docker-compose.yml`: Postgres 18 (`5535`; `llm_corpus` + `llm_generation`, `rag_app_login`), PgBouncer (`5536`, transaction mode), Valkey (`6381`), Qdrant (`6335`), floci (`4568`; bucket, two topics, six queues, `llm-events-probe-q` subscribed to both topics), floci-ui (`4501`; counts only). `TEMPORAL_ADDRESS` stays unset locally, so async routes answer `503 WORKFLOW_ENGINE_UNAVAILABLE` by design; the Model Gateway is opt-in (`make gateway-up`: Agent Router `envoyproxy/ai-gateway-cli:v1.2.0` on :1975 in front of the model stub on :8090, config in `deploy/model-gateway/aigw.yaml`); without it, model-calling routes answer 503.

## 16. Testing

Layers and markers in [`CONTRIBUTING.md`](../CONTRIBUTING.md#testing-requirements). Coverage ≈ 97.8% (unit + integration), gate 95% (`.github/scripts/coverage-gate.sh`). e2e runs separately (`make test-e2e`, Ryuk disabled in CI).

## 17. Known gaps

See the CHANGELOG *Known gaps*: no Worker Pool, no real peer exercised, Glue encoder, `/ready` and the relay, unbuilt sweeps, scope-cache invalidation, tenant purge reaching Valkey and evaluation samples, metrics domain, image pipeline.

## 18. Integration points

| Peer | Direction | What | Failure behaviour |
|---|---|---|---|
| Model Gateway | outbound | seven task classes over HTTP | lexical-only retrieval; generation `503` |
| Tender | outbound / inbound | packs (pulled, cached); public routes; lifecycle events | cached pack serves; uncached fails closed |
| IAM | outbound / inbound | memberships, LLM context, TAC-4; `TenantStateChanged`, `TenantMembershipsPurged` | TAC-4 refuses (CG-4); context omitted |
| Ingestion | inbound | C-I routes; `DocumentPurged` | — |
| Worker Pool | inbound | activity writes under `rag_activity`; start/signal contract | async routes `503` while unset |
| Temporal | outbound | start and signal | `WORKFLOW_ENGINE_UNAVAILABLE` |
| Qdrant, Valkey, S3 | outbound | vectors, caches, content | Qdrant `503`; Valkey miss; S3 content `503` |
| Usage & Metering | inbound | `TenantQuotaRestored` | — |
