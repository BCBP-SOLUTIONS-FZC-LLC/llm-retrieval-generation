# Key Files to Know

Critical implementation files for `llm-retrieval-generation`. See also [`architecture.md`](architecture.md) for the directory tree and layer rules.

- **`src/rag/presentation/main.py`** — `create_app(settings)`: asserts `PG_BOUNCER_MODE` is exactly `true`, builds the app with `create_platform_app`, registers routers and docs, and the lifespan that opens both pools, optionally applies the outbox schema, builds the `Container`, and starts registered background workers (relays and consumers when `EVENTS_ENABLED`). Workers stop in reverse order before the pools close.
- **`src/rag/presentation/dependencies.py`** — `Container` and `build_container`: one wiring point for corpus, retrieval, generation, workflow and scope services, the two pools, the Gateway client and the S3 store; `aclose` drains generation producers (bounded by `REQUEST_TIMEOUT_SECONDS`) before closing the Gateway client and the pools. The per-module `*_dependencies.py` files build each module's services.
- **`src/rag/presentation/identity.py`** — `require_identity` (public) and `require_tenant` (internal): identity only from gateway headers (API-1); `ExtraIdentityMiddleware` carries departments, plan, feature flags and residency mode.
- **`src/rag/presentation/errors.py`** — `DomainError` to RFC 9457; the §24 status table. A new `ErrorCode` needs an `ErrorSpec` here and a row in `tests/unit/test_error_taxonomy.py`.
- **`src/rag/presentation/api_docs.py`** — the docs routes; reads `docs/swagger/*` and `api/*` from the repo root, so those files must ship with the package.
- **`src/rag/domain/shared/errors.py`** — the 32 `ErrorCode`s and `DomainError`.
- **`src/rag/domain/generation/{run,section,artefact}.py`** — the run, section and artefact state machines. `GenerationRun.revoke_scope` writes edges the LLD tables lack (`pending`/`outline_building`/`suspended → failed`) because ASYNC-15 and LD-62 need them.
- **`src/rag/application/corpus/resolve.py`** — C-I1 resolve-or-create; emits `CollectionProvisioned`. Concurrent first documents resolve on a deterministic UUIDv5 primary key.
- **`src/rag/application/generation/flow.py`** — `AdmitGeneration` (the §5.6 admission order, then one transaction: idempotency lookup, shell, run, section, `GenerationStarted`), `RunLifecycle`, `CompleteGeneration` (version, section version, report, claims, completeness, audit entry, `GenerationCompleted`, in one transaction; S3 put inside it).
- **`src/rag/application/generation/generate.py`** — `ShortFormEngine`: `start()` runs everything that can still be an HTTP problem; a producer task drafts, verifies and writes; the SSE body only drains its queue (GEN-2).
- **`src/rag/application/verification/verify.py`** — `VerifySection`: the tiered ladder, evidence tier, completeness; returns `RETRY` without acting on it.
- **`src/rag/application/retrieval/pipeline.py`** — scope → pack → refine → plan → embed → search → fusion → rerank → assembly; degradation markers; one retrieval set per widened-retry attempt.
- **`src/rag/application/workflows/{start,signals,state,operator}.py`** — W-1..W-8: admission commits first, engine call second, signals after the row commit; guarded gate `UPDATE`; scope re-resolution on every signal.
- **`src/rag/corpusscope/{resolver,filters,token}.py`** — `ScopeResolver` (four branches, fail closed, 60 s cache), every Qdrant filter, `ScopeToken` (canonical hash, 600 s). Nothing else builds a Qdrant filter.
- **`src/rag/infrastructure/persistence/pools.py`** — `request_guc_provider` (request context, falling back to `guc_set_from_context` outside a request) and `bind_tenant_scope(tenant: UUID)`; a non-UUID raises `TypeError` (LD-19).
- **`src/rag/infrastructure/persistence/migrate.py`** — `migrate_all`: outbox schema on each database **first**, then the numbered sets.
- **`src/rag/infrastructure/events/publisher.py`** — `OutboxEventPublisher`: fails closed on an unregistered type or off-schema payload (EVT-9).
- **`src/rag/infrastructure/events/routing.py`** — `RoutingPublisher`: topic by event type name; never raises on an unknown type (it would dead-letter and delete the row), logs and counts instead.
- **`src/rag/infrastructure/events/{relay,consumers,runtime}.py`** — one claim loop per database; five consumers (tenant bound from a UUID, contract check, one use case, ledger row, poison to DLQ at the fourth receive); `start_consumers` only for queues whose URL is set.
- **`src/rag/infrastructure/gateway/client.py`** — `HttpModelGateway`: embed (300 ms, one retry, breaker), rerank (no retry), complete, SSE stream, tokenize with an over-estimate fallback. **The paths and header names are settings (`gateway_*`), defaulting to Agent Router's; this file is the only place that knows them.**
- **`src/rag/infrastructure/sweeps/runner.py`** — pools, per-instance advisory lock, `build_sweep`, `run_sweep`; `__main__.py` maps outcomes to exit codes 0/1/2/64.
- **`src/rag/infrastructure/storage/artefact_content_store.py`** — tenant-prefixed keys, SSE-KMS with the tenant in the encryption context, batched prefix delete.
- **`src/rag/application/events/contracts.py`** — the 17 published and 11 consumed event contracts (stdlib only); `infrastructure/events/schemas.py` derives the JSON Schemas.
- **`src/rag/infrastructure/config.py`** — `Settings` (every LLD §17 variable) and `assert_isolation_config`.
- **`scripts/gen_api_docs.py`** — generates `docs/swagger/swagger.{json,yaml}`, `api/asyncapi.{yaml,html}` and checks drift (`--check`); pins `app_name` so `APP_NAME` in the environment cannot change the output.
- **`migrations/README.md`** — numbering, ledger, DDL conventions; read before adding a migration.
- **`tests/support_*.py`** — in-memory fakes that emulate the unique indexes and the version trigger; extend them when a use case needs a new port.
- **`tests/e2e/conftest.py`** — the e2e harness: real app, real RLS Postgres, non-superuser login, `Stack` with `.client`, `.scope`, `.sql(instance, query)`.
