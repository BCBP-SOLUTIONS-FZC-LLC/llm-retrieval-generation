# Architecture

Layer rules, directory tree and ports for `llm-retrieval-generation`. Diagrams are in [`ARCHITECTURE.md`](../ARCHITECTURE.md) and `docs/architecture/mermaid/`.

## Directory tree

```
src/rag/
  domain/                      stdlib only
    shared/                    enums, errors (ErrorCode, DomainError), identity (RequestIdentity)
    corpus/                    collection, grant, index_generation, retention, tenant_lifecycle, repositories
    retrieval/                 scope, candidate, fusion (RRF), sufficiency, budget, plan, text
    generation/                artefact, section, run, evidence, verification, extraction, facts, instruction,
                               regeneration, refinement, prompt, eligibility, audit, views, repositories
    verification/              claims, tiers, scoring, completeness, ports
    workflows/                 ids, input, gate, views, repositories
    packs/                     profile, generation (Domain Profile / Language Pack value objects)
  application/                 imports domain only
    corpus/                    resolve, generations, lifecycle, retention, queries, events, ports
    retrieval/                 pipeline, search, refiner, classify, required_input, record, scope, ports
    generation/                flow (AdmitGeneration, RunLifecycle, CompleteGeneration), generate (ShortFormEngine),
                               extract, regenerate, refiner, queries, frames, requests, ports
    verification/              verify (VerifySection), gateway (entailer, judge, resampler)
    workflows/                 start, signals, state, operator, common, bridges, observability, ports
    packs/                     resolve, ports
    events/                    contracts (stdlib only), consumers (use cases), ports
    sweeps/                    sweeps (ten sweeps)
    shared/                    events (DomainEvent, EventPublisher), gateway ports, scope, observability, ports
  infrastructure/              imports application, domain, SDKs
    persistence/               pools, migrate, transactions, corpus/, generation/ (repositories, writes, ops, workflows)
    gateway/                   client (HttpModelGateway), model_catalogue
    scope/                     adapter (CorpusScopeAdapter), store
    retrieval/                 qdrant, lexical adapters
    tender/ iam/ temporal/     pack resolver and codec; principals, llm_context, TAC-4; workflow client and factory
    cache/ storage/            Valkey cache and invalidation; S3 content store
    events/                    publisher, routing, relay, consumers, runtime, schemas
    sweeps/                    runner, __main__
    observability/             metrics, generation_metrics, workflow_metrics, logs
    config.py                  Settings, assert_isolation_config
  presentation/                imports application, domain, infrastructure config
    api/{public,internal,operator}/   routers + *_models.py; sse.py
    dependencies.py, *_dependencies.py, errors.py, identity.py, api_docs.py, main.py
  corpusscope/                 stdlib + pydantic + qdrant_client + itself: token, resolver, filters, qdrant
migrations/{corpus,generation}/   NNNN_name.up.sql + .down.sql
schemas/events/                   derived JSON Schemas
api/ docs/swagger/                generated contracts
scripts/                          gen_api_docs.py, asyncapi_page.{py,css,js}, init-db.sql, init-floci.sh
tests/                            unit usecase api events isolation architecture repository integration e2e
```

## Layer rules

| Layer | May import |
|---|---|
| `domain` | standard library, `rag.domain` |
| `application` | standard library, `rag.domain`, `rag.application`, and `fastapicommon.logging` only (`events/contracts.py`: standard library only) |
| `infrastructure` | everything except `presentation` |
| `presentation` | everything |
| `corpusscope` | standard library, pydantic, `qdrant_client`, itself |

DEP-1 `qdrant_client` only in `corpusscope`. DEP-2 no HTTP client toward an inference backend outside `infrastructure/gateway`, and `gateway_base_url` is read only in the gateway adapter, settings and dependency wiring. DEP-3 `corpus` imports none of `retrieval`, `generation`, `verification`. DEP-4 `verification` never imports `retrieval`. DEP-5 (no write to Ingestion's tables) is held by role grants.

## Ports (outbound capabilities, defined in `application/`)

| Port | Implemented by |
|---|---|
| `TransactionRunner` | `PgTransactionRunner`, `CorpusTransactionRunner` (503 on an unreachable store) |
| `EventPublisher` | `OutboxEventPublisher` (fails closed on an unregistered type or off-schema payload) |
| `ModelGateway`, `TokenCounter` | `HttpModelGateway` (`embed`, `rerank`, `complete`, `generate_stream`, `count`) |
| `ScopeAuthorizer`, `ScopeProvider`, corpus `ScopeResolver`, `PointCounter` | `CorpusScopeAdapter` |
| `ArtefactContentStore` | `S3ArtefactContentStore` |
| `WorkflowClient` | Temporal client adapter; `UnwiredWorkflowClient` when `TEMPORAL_ADDRESS` is empty |
| `PackResolver`, `PackCache` | Tender resolver + Valkey cache |
| `UserContext` (IAM `llm-context`), principals (IAM memberships), `TenderAccess` (TAC-4) | `HttpUserContext`, `infrastructure/iam/*`; `UnconfiguredTenderAccess` when no IAM URL |
| `ProfileCache`, `PlacementCache` | `ValkeyProfileCache`, `ValkeyPlacementCache` |
| `PipelineObserver`, `GenerationObserver`, `WorkflowObserver` | Null observers wrapped by logging/tracing, plus Prometheus when enabled |

Every external adapter **fails closed**: unconfigured peers are wired to an `Unwired*` / `Unconfigured*` implementation that refuses rather than answers.

## Shared libraries

| Library | Used for |
|---|---|
| `platform-fastapicommon` 0.4.x | `create_platform_app`, RFC 9457, `RequireRole` / `RequireAnyRole`, tracing, logging, metrics registry |
| `pgcommon` 0.4.x | asyncpg `Pool`, tenant GUC provider, `run_in_tx`, `Runner` (migrations), pool metrics |
| `eventcommon` 0.4.x | `Envelope`, outbox (`apply_schema`, relay), `SQSConsumer`, DLQ publisher, inbox `Store` and `processed_events`, `GlueDecodeCodec`, `eventcommon.config` (all tuning); built in `infrastructure/events/config.py` |

Private packages: installed via the index configured for `uv` (`CI_REPO_READ_TOKEN` in CI), as in `platform-fastapicommon`.
