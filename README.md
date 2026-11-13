# llm-retrieval-generation

The Retrieval & Generation Service of the LLM subsystem — the **only component that turns a tenant's indexed documents into grounded answers, drafts, extractions and long-form artefacts**, and the system of record for what was retrieved, what was generated, and how well each claim was supported. It owns the corpus *collections* and their access grants, the retrieval record, the generation record (artefacts, versions, sections, runs), verification verdicts, and the audit chain over generated content. It resolves every caller's search scope, fails closed when it cannot, and never lets one tenant's chunk reach another tenant's prompt.

**Repository:** `github.com/BCBP-SOLUTIONS-FZC-LLC/llm-retrieval-generation`
**Stack:** Python 3.15 · FastAPI · asyncpg (`pgcommon`) · `eventcommon` · `platform-fastapicommon` · deployed as one service plus CronJob sweeps from one image (image pipeline is planned — see [Deployment](#deployment))
**Design:** the LLD, `docs/lld/llm-lld-retrieval-generation-service.md`; where the LLD and the LLM HLD disagree, the HLD is authoritative. Build status, deviations and the traceability matrix are in [`docs/implementation-plan.md`](docs/implementation-plan.md).

> **Status: pre-release (`v0.x`), never deployed.** All 35 routes are mounted and tested against fakes, a real Postgres 18 and an e2e stack. The Worker Pool, the Model Gateway and the other peer services are not wired, so long-form generation and anything that calls a model cannot run end to end. See [Out of scope](#out-of-scope) and [`VERSIONING.md`](VERSIONING.md).

---

## Mental model

| This service owns | It does NOT own |
|---|---|
| `collections`, `collection_grants`, `index_generations`, `retention_policies`, `tenant_lifecycle` (`llm_corpus`) — placement, grant overlay answers, activation, retention, tenant freeze/purge | Documents, chunks, vectors, embeddings, the lexical index, parsing and chunking (Ingestion) |
| `artefacts`, `artefact_versions`, `artefact_sections`, `artefact_section_versions`, `generation_runs` (`llm_generation`) — the generated record | The Tender section/review state machine — a section failing is artefact state, not Tender work state |
| `retrieval_sets`, `retrieval_set_chunks`, `query_refinements`, `instruction_refinements` — what was retrieved and refined | Model weights, provider credentials, routing, residency enforcement, quota (the Model Gateway) |
| `groundedness_reports`, `claim_verifications`, `completeness_checks`, `document_facts`, `fact_conflicts`, `artefact_audit_chain` | Workflow and activity **logic**, including the LangGraph graphs (the Worker Pool; this service starts and signals the run and owns the tables its activities write) |
| `processed_events` ledger, the outbox on both databases, `corpusscope` (scope resolution and every Qdrant filter) | Users, tenants, roles, memberships (IAM — this service is identity-agnostic and stores only UUID references) |
| Ten sweeps (orphans, stalls, idempotency keys, retrieval sets, checkpoints, outbox, ledger, audit chain, tenant purge, promotions) | Domain Profiles and Language Packs (Tender — pulled and cached, never stored) |

Every request carries identity as `x-*` headers injected by the gateway after authentication; the service trusts nothing else (API-1). Tenant isolation is enforced by Postgres row-level security (`FORCE` on, no `BYPASSRLS` role) **and** by a mandatory compound Qdrant filter built in one place (`corpusscope`).

---

## Why this service exists

- **One authority for scope.** "Which collections may this caller read, at what level, right now" has one answer, with the purge-epoch and restricted-tender rules (`SCOPE-*`, `COL-*`) enforced once instead of by every caller.
- **Grounding is a contract, not a prompt.** Every answer and draft is verified in tiers (self-consistency, span overlap, entailment, judge) and ends in a verdict frame carrying an evidence tier (`NOT_COMPUTED`, `INSUFFICIENT`, `PARTIAL`, `SUPPORTED`); an ungrounded claim is surfaced, never silently shipped.
- **Reproducibility and erasure.** Every artefact keeps its retrieval set, model, prompt hash and an append-only audit chain, and every artefact object lives under a tenant prefix so a tender purge or tenant offboarding deletes all of it (`ART-9`).
- **Fail closed.** No scope store, no IAM grant answer, no workflow engine: the request is refused (`503` with a specific code) rather than answered with a default.

---

## API overview

35 routes in four prefixes. The generated contracts are committed: [`docs/swagger/swagger.json`](docs/swagger/swagger.json), [`api/asyncapi.yaml`](api/asyncapi.yaml), [`schemas/events/`](schemas/events/). Identity headers: `x-tenant-id`, `x-user-id`, `x-tenant-roles` (public); `x-tenant-id` only (internal).

### Public routes (27) — `/api/v1/*`

| ID | Method and path | Purpose | Role |
|---|---|---|---|
| R-1 | `POST /search` | Hybrid retrieval, results only; `206` where a stage degraded | member |
| R-2 | `POST /answers` | Grounded answer, streamed as `draft` frames then one `verdict` (SSE) | member |
| R-3 | `POST /sections/drafts` | Interactive section draft, same transport as R-2 | member |
| R-4 | `POST /extractions` | Structured extraction, synchronous; `206` where degraded | member |
| R-5 | `GET /retrieval-sets/{id}` | The recorded set with its chunks (purged ones flagged) | member |
| R-6 | `POST /classification/propose` | Stateless role proposal; no scope resolved | member |
| R-7 | `POST /required-input-checks` | The pack's required-input rules against the resolved scope | member |
| A-1 | `GET /artefacts/{id}` | Artefact with current version, tier and eligibility | member |
| A-2 | `GET /artefacts/{id}/versions` | Version history, newest first | member |
| A-3 | `GET /artefacts/{id}/versions/{no}` | One version with per-section groundedness | member |
| A-4 | `GET /artefacts/{id}/sections` | Section states and evidence tiers | member |
| A-5 | `POST /artefacts/{id}/regenerate` | Document-scope regeneration; `202` + handle | member |
| A-6 | `POST /artefacts/{id}/sections/{ref}/regenerate` | Section regeneration, streamed like R-3 | member |
| A-7 | `POST /artefacts/{id}/sections/{ref}/fact-conflict-resolution` | Resolve a surfaced fact conflict | member |
| A-8 | `GET /artefacts/{id}/render` | **Reserved**: `503 RENDERER_UNAVAILABLE` | member |
| A-9 | `GET /artefacts/{id}/sections/{ref}/versions/{no}/content` | Generated text of one section version | member |
| W-1 | `POST /generations` | Start a long-form run; `202` + handle, `200` on idempotent replay | member |
| W-2 | `GET /generations/{id}` | State, outline, per-section progress (never read from Temporal) | member |
| W-3 | `POST /generations/{id}/outline/approve` | Approve the outline | member |
| W-4 | `POST /generations/{id}/outline` | Submit a revised outline | member |
| W-5 | `POST /generations/{id}/cancel` | Cancel; completed sections stay readable | member |
| W-6 | `POST /extractions/async` | Extraction beyond R-4's budget, as a workflow | member |
| W-7 | `POST /generations/{id}/resume` | Resume a run suspended on quota | member |
| P-1 | `GET /collections` | Collections in the caller's resolved scope | member |
| P-2 | `GET /collections/{id}` | One collection; absent and ungranted are both 404 | member |
| P-3 | `GET /collections/{id}/grants` | Last recorded overlay answer, for diagnosis | `tenant_admin` or `tender_admin` |
| P-6 | `PUT /collections/{id}/retention` | Create (`201`) or update (`200`) retention; stale version `409` | `tenant_admin` |

### Internal routes (6) — `/api/v1/internal/collections/*`, in-mesh only (Ingestion)

| ID | Method and path | Purpose |
|---|---|---|
| C-I1 | `POST /resolve` | Resolve **or create** a tender's collection; `201` created, `200` existing |
| C-I2 | `GET /{id}` | Placement and generation metadata, in every status |
| C-I4 | `POST /{id}/index-generations/{n}/activate` | Atomic cutover of a verified generation |
| C-I5 | `GET /{id}/retention` | The policy the retention sweep applies |
| C-I6 | `POST /{id}/purge-epoch` | Atomic purge-epoch increment |
| C-I8 | `POST /{id}/migration-result` | Promotion copy result; `complete` triggers the cutover |

Reserved, not mounted: `C-I3`, `C-I9`. Retired: `C-T1`, `C-T2`.

### Operator (1) and catalogue (1)

| ID | Method and path | Purpose |
|---|---|---|
| W-8 | `POST /api/v1/operator/generations/{id}/rematerialise` | Restart a lost run from its row; audited; `platform_operator` only |
| C-M1 | `GET /api/internal/models` | Model ids a tenant may select; `503 INFERENCE_UNAVAILABLE` where unreadable |

### Health and documentation

`GET /health`, `/live`, `/ready` · `GET /` (landing), `/swagger` (Swagger UI, BCBP theme), `/swagger/doc.json|yaml`, `/asyncapi` (event catalogue), `/asyncapi.yaml`, plus FastAPI's own `/docs` and `/openapi.json`.

### Errors

RFC 9457 `application/problem+json` through `fastapicommon`: `errorCode` (upper snake case), `traceId`, `tenantId`, `timestamp`, `validationErrors`. 32 domain codes (`src/rag/domain/shared/errors.py`, LLD §24), mapped to statuses in `presentation/errors.py`.

---

## Architecture

Layers (user-mandated) crossed with six LLD modules (`corpus`, `retrieval`, `generation`, `verification`, `workflows`, `packs`) plus `corpusscope`:

```
src/rag/
  domain/          entities, value objects, enums, domain services, repository ports (stdlib only)
  application/     commands, queries, DTOs, use cases, outbound ports, event contracts
  infrastructure/  persistence (asyncpg), gateway, tender, iam, temporal, cache, storage, events, sweeps, observability
  presentation/    FastAPI routers (public / internal / operator), dependencies, errors, app factory
  corpusscope/     scope resolution + every Qdrant filter (vendored library; sole importer of qdrant_client)
migrations/{corpus,generation}/   numbered SQL sets, one per database
schemas/events/                   JSON Schemas derived from the event contracts
```

### Dependency rules (enforced by `tests/architecture/test_dependency_rules.py`)

- `domain` imports the standard library only; `application` imports `domain` (and, from the platform, only `fastapicommon.logging`); `infrastructure` and `presentation` point inward.
- **DEP-1** only `corpusscope` imports `qdrant_client`; **DEP-2** no module outside `gateway` builds an HTTP client toward an inference backend; **DEP-3** `corpus` imports none of `retrieval`, `generation`, `verification` (a leaf, extractable later); **DEP-4** `verification` never imports `retrieval` (it receives the retrieval set as data).
- **DEP-5** (no write to Ingestion's document, chunk, vector or lexical tables; no Qdrant write grant) is held by database role grants, not statically.
- **CI-1** every route declares an authentication dependency (C-M1 is the one named exemption).

### Storage and messaging

| Store | Use |
|---|---|
| PostgreSQL 18 ×2 — `llm_corpus`, `llm_generation` | System of record, behind PgBouncer in **transaction mode** (`PG_BOUNCER_MODE` must be exactly `true`); RLS on every table except `processed_events` and the outbox |
| Qdrant | Vectors, read through `corpusscope` only |
| Valkey | Scope, pack, retrieval and placement caches; every key tenant-prefixed; an outage is a miss, never an error |
| S3 | Section text under `tenants/<tenant>/artefacts/<artefact>/…`, SSE-KMS with the tenant in the encryption context |
| SNS / SQS | Outbox relay publishes to `llm-generation-events` and `llm-index-events`; six consumer queues |
| Temporal | Started and signalled only; no workflow logic here |

### Database roles

`rag_app` (request handlers), `rag_activity` (Worker Pool activities, INSERT-limited where noted), `rag_owner` (DDL), `rag_sweeper` (column-limited cross-tenant enumeration for sweeps). Migrations apply the outbox schema **before** the numbered sets (`0003` grants on tables the library owns).

### Shared library dependencies

| Library | Used for |
|---|---|
| `platform-fastapicommon` 0.4.x | App factory, RFC 9457 errors, RBAC dependencies, tracing, logging, metrics registry |
| `pgcommon` 0.4.x | asyncpg pool, tenant GUC provider for RLS, `run_in_tx`, migration runner |
| `eventcommon` 0.4.x | Envelope, transactional outbox and relay, SQS consumer, DLQ forwarding, inbox ledger |

---

## Integrating with other services

1. **Tender** calls the public routes (R-, A-, W-, P-), serves Domain Profile / Language Pack resolution (pulled, cache-fronted), publishes `TenderRestricted` / `TenderPurged` and the four terminal outcomes, and consumes `llm-generation-events`.
2. **Ingestion** calls the C-I routes. It creates the Qdrant collections, builds and verifies index generations, and reports each state to this service, which writes it. Promotion point-copy (C-I7) and source purge (C-I10) are *required of Ingestion*, not routes here.
3. **IAM** is called for principal memberships (`GET /users/:id/memberships`), the user LLM context, and the Tender ACL overlay (TAC-4); it publishes `TenantStateChanged` and `TenantMembershipsPurged`.
4. **Model Gateway** (Agent Router, formerly Envoy AI Gateway; run by this team) is the only path to inference: seven task classes (`embed`, `rerank`, `draft`, `judge`, `extract`, `refine-query`, `refine-instruction`). `preferred_model` is a hint; `x-residency-mode` is forwarded on every call.
5. **Worker Pool** runs long-form workflows and their LangGraph steps; the contract is [`docs/worker-pool-contract.md`](docs/worker-pool-contract.md).
6. **Usage & Metering** emits `TenantQuotaRestored`, which resumes suspended runs.

Peer contracts that the LLD does not fix (Tender pack JSON, IAM bodies, Gateway routes) are **assumptions**, listed per slice in the implementation plan; verify them against the owning team before relying on them.

### Events

Published (17): `llm-generation-events` — `GenerationStarted`, `GenerationGapReported`, `OutlineReady`, `SectionCompleted`, `GenerationProgressed`, `GenerationCompleted`, `GenerationFailed`, `GenerationCancelled`, `ArtefactRegenerated`, `ExtractionCompleted`; `llm-index-events` — `IndexGenerationActivated`, `IndexGenerationFailed`, `CorpusConflictDetected`, `CollectionProvisioned`, `CollectionPromoted`, `CollectionStatusChanged`, `ProfileCacheRefreshed`.

Consumed (11 types on 5 queues): `llm-document-rag-q`, `domainprofile-llm-invalidate-q`, `membership-rag-q`, `tender-rag-q`, `usage-rag-q`. Poison messages go to the queue's DLQ at the fourth receive and are acknowledged.

Events are written to the outbox **in the same transaction** as the state change (EVT-8) and relayed by one claim loop per database through a routing publisher. An unregistered type or an off-schema payload fails closed inside that transaction.

---

## Local development

### Prerequisites

Python 3.15+ (`make setup` has `uv` fetch it; asyncpg 0.31 has no 3.15 wheel yet, so a C compiler is needed to build it), Docker (for the compose stack and the integration/e2e tests), `make`. `uv` is installed into `.venv` by `make setup` if it is missing (pinned `0.13.1`).

### Setup

```bash
make setup        # .venv + uv sync --locked; copies .env-example to .env
make docker-up    # postgres x2 DBs, pgbouncer, valkey, qdrant, floci, floci-ui
make migrate      # outbox schema, then both numbered migration sets
make run          # API on :8080 (kills anything already listening there)
```

`.env` is gitignored; its defaults match `docker-compose.yml`'s host ports.

### Common commands

| Command | What it does |
|---|---|
| `make test` | Unit + use-case + architecture + API tests (no Docker) |
| `make test-ci` | Unit + integration with coverage, the CI run (needs Docker) |
| `make test-integration` | Migration and Postgres tests (needs Docker) |
| `make test-e2e` | Real app over a real RLS Postgres (needs Docker) |
| `make ci` | `lock-check` + `fmt-check` + `lint` + `typecheck` + `rls-check` + `swag-check` + `test-ci` + `build` |
| `make cover-check` | `test-ci`, then the coverage gate (`COVERAGE_THRESHOLD`, default 95) |
| `make rls-check` | Fail on a non-`LOCAL` `SET app.tenant_id` in `src/` or `migrations/` (RLS-6; CI calls it) |
| `make build` | Build the sdist and wheel into `dist/` (`uv build`) |
| `make fmt` / `lint` / `typecheck` | `ruff` fix and format / `ruff check` / `mypy --strict src` |
| `make extract-schemas` | Derive `schemas/events/*.json` from the event contracts |
| `make swag` / `swag-check` | `extract-schemas` + regenerate / verify the OpenAPI, AsyncAPI and event schemas |
| `make lock` / `lock-check` | Refresh / verify `uv.lock` |
| `make vuln-check` | `pip-audit` on the locked dependencies |
| `make trivy` | Build the image, then Trivy-scan it as `release.yml` does (CRITICAL/HIGH with a fix fail); local `trivy` if installed, else the `TRIVY_IMAGE` container (Docker) |
| `make sweeps` | Run the sweep runner locally |
| `make events-peek` | Print the events waiting in the dev probe queue |
| `make gateway-up` / `gateway-down` | Start / stop the model gateway (Agent Router, pinned image) in front of a model stub (opt-in) |
| `make gateway-up-ollama` / `ollama-down` | Put real local models behind the gateway: Ollama (`alpine/ollama`) serves `embed`, `draft`, `judge`, `extract` and both refiners (`qwen2.5:1.5b`, `llama3.2:1b`, `nomic-embed-text`); `rerank` stays on the stub. The first run pulls about 2.5 GB. `make test-gateway` needs the stub config, so run `ollama-down` first |
| `make gateway-up-vllm` / `vllm-down` | Put `embed` and `rerank` on vLLM (CPU image, `BAAI/bge-small-en-v1.5` and `cross-encoder/ms-marco-MiniLM-L-6-v2`) behind the gateway, config `deploy/model-gateway/aigw-vllm.yaml`; the other classes stay on the stub. Tests the self-hosted contract (LLD OQ-93). Measured at about 2.6 GB for the two containers, so stop Ollama first on a 4 GB Docker VM; `vllm-down` returns the gateway to the stub |
| `make chat-up` / `chat-down` | Start / stop Open WebUI (`ghcr.io/open-webui/open-webui:v0.6.43`) wired to the gateway; needs `gateway-up`; the first pull is several GB |
| `make test-gateway` | Our gateway client against that gateway: contract tests (needs `gateway-up`) |
| `make schema-validate` | `extract-schemas`, then validate `schemas/events/` with `platform-schemagov` (Docker) |
| `make docker-down` | Stop the containers |

Single test: `.venv/bin/python -m pytest tests/unit/test_config.py::test_env_overrides_by_upper_case_name -q`. Integration and e2e tests are opt-in by marker (`-m integration`, `-m e2e`).

### Local ports

| Service | Host port |
|---|---|
| API | 8080 |
| PostgreSQL (direct, migrations) | 5535 |
| PgBouncer (application DSNs) | 5536 |
| Valkey | 6381 |
| Qdrant | 6335 |
| floci (S3 / SNS / SQS) | 4568 |
| floci-ui | 4501 |
| Model gateway (opt-in, `make gateway-up`) | 1975 (admin 1064) |
| Open WebUI chat over the gateway (opt-in, `make chat-up`): http://localhost:3000 | 3000 |
| Model stub (opt-in; the gateway's backend, no UI) | 8090 |

### Calling the API locally

Open http://localhost:8080/swagger, click **Authorize**, and set `TenantID` (any UUID). Or with curl:

```bash
# C-I1: resolve or create a tender's collection (internal route: x-tenant-id only)
curl -s -X POST localhost:8080/api/v1/internal/collections/resolve \
  -H "x-tenant-id: 11111111-1111-1111-1111-111111111111" \
  -H "content-type: application/json" \
  -d "{\"tender_id\":\"$(uuidgen | tr A-Z a-z)\"}"
```

Public routes also need `x-user-id`, and some need `x-tenant-roles: tenant_admin`. Routes that call Tender, IAM or the workflow engine answer `503` locally by design — there is no Temporal in the compose stack. Routes that call a model also answer `503` until you run `make gateway-up` and set `GATEWAY_BASE_URL=http://localhost:1975` in `.env`: that starts Agent Router in front of the **model stub** (deterministic answers, failures on command), so the routes run end to end without a real model. To see it by hand, `make chat-up` starts **Open WebUI** at http://localhost:3000 (no sign-in), pointed at the gateway: pick a task class as the model and type; the picker lists whatever the gateway's `GET /v1/models` returns, so each class has its own exact route in `deploy/model-gateway/aigw.yaml`. With `make gateway-up-ollama` the same picker talks to real models (class to model in `deploy/model-gateway/aigw-ollama.yaml`). The **Document editor** model is a preset on `draft` (created by `make chat-up` from `scripts/open-webui/document_mode.py`): it always answers with the whole document in one `html` block, which Open WebUI shows in its Artifacts side panel, so each edit request ("make the second section a bullet list") shows the revised document beside the chat. It is not a true canvas: you cannot type in the panel, only ask for changes, and the small local models sometimes alter parts you did not mention. `embed` and `rerank` are listed but are not chat models, so use `draft`, `judge`, `extract`, `refine-query` or `refine-instruction` there. The gateway itself has no web interface: port 1975 is the API and 1064 serves only `/health` and `/metrics`.

---

## Testing domain events locally

C-I1 is the simplest route that emits an event (`CollectionProvisioned`). The outbox relay publishes it to the `llm-index-events` SNS topic, and a dev **probe queue** (`llm-events-probe-q`, created by `scripts/init-floci.sh`) is subscribed to both topics so you can see what was published.

1. In `.env` set `EVENTS_ENABLED=true` (optionally `OUTBOX_POLL_INTERVAL=1s`), then `make run`.
2. Call C-I1 with a **fresh** `tender_id` (a repeat returns `200` and emits nothing).
3. In floci-ui (http://localhost:4501) open SQS → `llm-events-probe-q`: the message count rises by one. floci-ui shows counts only, not message bodies.
4. `make events-peek` prints the event envelopes (`type`, `source: llm-rag-service`, `tenant_id`, `subject`, `data`). It does not consume them.
5. To empty the queue: `aws sqs purge-queue` through the floci container (`docker compose exec floci aws --endpoint-url http://localhost:4566 …`).

Troubleshooting: count stays 0 → `EVENTS_ENABLED` is still false or the app was not restarted; `401/403` → missing `x-tenant-id`; `200` instead of `201` → that `tender_id` already exists.

---

## Testing

~1,270 tests (1,238 unit + integration, 32 e2e); coverage about 97.8% for unit + integration.

| Group | Where | Needs |
|---|---|---|
| Unit, use-case, API, architecture, events, isolation | `tests/unit`, `usecase`, `api`, `architecture`, `events`, `isolation` | Nothing (fakes) |
| Repository and migration (RLS, triggers, grants) | `tests/repository`, `tests/integration` (`-m integration`) | Docker (`postgres:18` via testcontainers) or `RAG_TEST_PG_DSN_BASE` |
| End to end | `tests/e2e` (`-m e2e`) | Docker; real app, real RLS Postgres, non-superuser login in `rag_app` |

### Canonical tests (do not break)

- `tests/architecture/test_dependency_rules.py` — layering and DEP-1..5.
- `tests/unit/test_api_docs.py` — committed specs equal generated output; docs routes served.
- `tests/unit/test_error_taxonomy.py` — the 32 error codes and their statuses.
- `tests/repository/*` and `tests/e2e/test_security.py` — cross-tenant isolation under `FORCE` RLS.
- `tests/unit/test_config.py` — every LLD §17 variable and default; the PgBouncer-mode startup refusal.

### Coverage

`make test-ci` writes `coverage.xml`. CI's gate (`.github/scripts/coverage-gate.sh`) fails below **95%**. Superusers bypass RLS even with `FORCE`, so tests that assert isolation use a non-superuser login.

---

## Environment variables

Names are the upper-cased `Settings` fields (`src/rag/infrastructure/config.py`); [`.env-example`](.env-example) is the working local set. The LLD §17 variables keep their exact names and defaults.

| Group | Variables |
|---|---|
| Databases | `CORPUS_DATABASE_URL`, `GENERATION_DATABASE_URL` (via PgBouncer, non-superuser), `CORPUS_MIGRATION_DATABASE_URL`, `GENERATION_MIGRATION_DATABASE_URL` (direct, DDL), `PG_BOUNCER_MODE` (**exactly** `true`), `PG_MAX_CONNS` (= PgBouncer pool size) Optional pool tuning is `pgcommon`'s own `PG_*` set, read from the process environment (`make run` exports `.env`): `PG_STATEMENT_TIMEOUT`, `PG_LOCK_TIMEOUT`, `PG_ACQUIRE_TIMEOUT`, `PG_SLOW_QUERY_THRESHOLD`, `PG_MAX_CONN_LIFETIME`, `PG_MAX_CONN_IDLE_TIME`, `PG_HEALTH_CHECK_PERIOD`, `PG_MIN_CONNS`; a bad value becomes the library's default plus a logged `pgcommon_config_warning`. |
| Sweeps | `CORPUS_SWEEP_DATABASE_URL`, `GENERATION_SWEEP_DATABASE_URL` (login roles in `rag_sweeper`; the runner refuses to start without them), `SWEEP_POOL_MAX_CONNS` |
| Stores | `VALKEY_URL`, `QDRANT_URL`, `QDRANT_API_KEY`, `S3_BUCKET`, `S3_REGION`, `S3_ENDPOINT_URL`, `S3_KMS_KEY_ID` |
| Peers | `GATEWAY_BASE_URL`, `GATEWAY_*_PATH` and `GATEWAY_*_HEADER` (the gateway's paths and header names; defaults are Agent Router's), `TENDER_BASE_URL`, `IAM_BASE_URL` (Org & Membership), `TENDER_ACL_BASE_URL` and `USER_PROFILE_BASE_URL` (each falls back to `IAM_BASE_URL` when empty), `TEMPORAL_ADDRESS` (empty: async routes answer `503 WORKFLOW_ENGINE_UNAVAILABLE`), `TEMPORAL_NAMESPACE`, `TEMPORAL_TASK_QUEUE*` |
| Events | `EVENTS_ENABLED`, `AWS_REGION`, `AWS_ENDPOINT_URL`, `GENERATION_TOPIC_ARN`, `INDEX_TOPIC_ARN`, the six `*_QUEUE_URL`, `OUTBOX_*`, `SQS_*` |
| Behaviour | the LLD §17 set: `SCOPE_TOKEN_TTL_SECONDS`, `RRF_K`, `RERANK_*`, `FANOUT_MAX_*`, `IDEMPOTENCY_TTL_SECONDS`, `REQUEST_TIMEOUT_SECONDS`, `DR_MODE`, … |
| Platform | `APP_ENV`, `APP_NAME`, `OBSERVABILITY_DOMAIN`, `ENABLE_METRICS`, `ENABLE_TRACING`, `PROBLEM_TYPE_BASE` |

`DR_MODE=true` disables the four destructive sweeps and tenant purge in the use case itself.

---

## Security

- **Identity** comes only from gateway-injected headers; there is no token parsing here (API-1, API-11). Internal routes authenticate by `x-tenant-id` alone and rely on mesh mTLS and NetworkPolicy.
- **Isolation** is Postgres RLS (`FORCE`, no `BYPASSRLS`) plus the mandatory Qdrant compound filter; absent and ungranted resources are both `404` (CG-8).
- **Prompt safety**: six-layer prompts with authority and cacheability per layer, a nonce boundary around retrieved content, and pack-regex input screening; the six layers never mix cross-tenant content into a cached prefix.
- **No content in logs or labels**: queries, chunk text and ids are never logged as message text, and metric labels are bounded vocabularies (OBS-2).
- **Erasure**: tenant-prefixed S3 keys and RLS-scoped rows make tender purge and offboarding complete (`ART-9`, `PE-*`).
- **Audit**: an append-only, gap-free hash chain per tenant (artefact audit entries), verified by a sweep that exits `2` on a break and is never self-healed.

Report vulnerabilities per [`.github/SECURITY.md`](.github/SECURITY.md).

---

## Observability

Logs, spans and metrics go through `fastapicommon` and nothing else. Every log line is written with `fastapicommon.logging.get_logger` (no module uses the standard library's `logging`), metrics and tracing are built in `infrastructure/observability` from `fastapicommon.metrics` and `fastapicommon.telemetry`, and `tests/architecture/test_shared_libs_only.py` fails the build on a direct logging, metrics or tracing SDK. Third-party libraries (httpx, asyncpg, boto3) still log through the standard library, and the platform's handler renders those records the same way. Tier-3 metrics are named `llm_retrieval_generation_<metric>` (LLD §16). **Metrics are off by default**: the `llm` domain is not registered in the platform metrics registry yet (OQ-103), so `ENABLE_METRICS` stays `false` and `OBSERVABILITY_DOMAIN` unset until it is. Spans cover each pipeline stage; an escaping error is recorded by type only.

---

## Deployment

**Partly planned.** A `Dockerfile` exists (`make docker-build`: digest-pinned `python:3.15.2-slim-bookworm`, `uv sync --locked`, non-root uid 10001, port 8080); a Helm chart exists in `deploy/helm/` (`make helm-check`; the IRSA policy is in `deploy/irsa/`) but has never been applied to a cluster, and `.github/workflows/release.yml` builds, scans, boot-tests, pushes and signs the image on a version tag (never run) — see the table in [`VERSIONING.md`](VERSIONING.md#planned-not-yet-in-place). The chart runs:

- One image carrying the API (`uvicorn --factory rag.presentation.main:create_app`), the migration entrypoint (`python -m rag.infrastructure.persistence.migrate`) and the sweep runner (`python -m rag.infrastructure.sweeps <name>|all`).
- Migrations run from a one-shot job, directly against Postgres (never through PgBouncer: the advisory lock is session-scoped), before the new image takes traffic, and must be safe while the previous image still serves.
- The `api/` and `docs/swagger/` files must ship with the package; `/asyncapi` and `/swagger` read them from the repo root.

---

## CI

`.github/workflows/ci.yml` runs two reusable gates and the image build, scan and smoke jobs on pushes to `main` and on pull requests (skipped for Markdown-only changes), then a PR summary comment.

| Gate | What runs |
|---|---|
| `validate-test` | `make test-ci` (unit + integration, Docker), the 95% coverage gate, then `make test-e2e` |
| `validate-quality` | lockfile check, `uv sync --locked`, `uv pip check`, the RLS-6 grep check, format, lint, `mypy --strict`, `pip-audit` |
| `build-image` / `trivy` / `smoke` | `Build image (cache)` lints the Dockerfile (Hadolint) and builds the image into the GHA and registry caches; `Trivy CVE scan` fails on a CRITICAL or HIGH finding with a fix (same settings as `release.yml` and `make trivy`); `Smoke tests` migrate, start and require `/ready` = 200 against Postgres + PgBouncer (`.github/scripts/image-boot-check.sh`) |
| `changelog-check` | A PR changing `src/`, `migrations/` or `schemas/` must update `CHANGELOG.md` |

Actions are pinned by SHA; private packages are installed with the `CI_REPO_READ_TOKEN` secret. `ci.yml` also pushes a signed `main` / `sha-` image to GHCR on merge. `release.yml` (on a version tag) validates, builds, CVE-scans, boot-tests, pushes, adds an SBOM and SLSA provenance, signs the image and publishes a GitHub Release (notes from `CHANGELOG.md`); `vuln-scan.yml` audits dependencies weekly; Dependabot proposes pip, Actions and Docker bumps. None of it has run on GitHub yet (see [`VERSIONING.md`](VERSIONING.md)).

**Merge gate:** `main` is the default branch and the organisation ruleset `protect-main-branch` governs it: a PR with a code-owner approval (not from the last pusher) and six checks, `Build image (cache)`, `Trivy CVE scan`, `Smoke tests`, `Validate / Quality / quality`, `Validate / Test / test` and `PR summary`, all produced by `ci.yml`. The checks have not run to completion on GitHub yet: Actions jobs did not start because of an account billing limit. `make trivy` and the pre-push hook are the local equivalent of the CVE scan.

### Git hooks and Claude Code automation

`make setup` (or `make install-hooks`) installs two hooks from `.githooks/`: `pre-commit` (`fmt-check`, `lint`, `typecheck`, `swag-check`, and `lock-check` when the lockfile inputs are staged) and `pre-push` (`make trivy`; needs Docker; skips branch deletions; `git push --no-verify` bypasses it).

For Claude Code, `.claude/settings.json` registers two hooks (`format-python.sh` runs `ruff format` on edited project `.py` files; `block-generated.sh` refuses edits to `docs/swagger/*`, `api/asyncapi.*` and `schemas/events/*.json`), `.claude/skills/` holds `pr-check` (the pre-PR gates, user-invoked) and `peer-contract-check` (background rule: peer contracts are assumptions until the peer's doc confirms them), and `.claude/agents/` holds read-only `security-reviewer` and `rls-reviewer`.

---

## Docker

### What the bundled `docker-compose.yml` starts

`postgres` (18; `llm_corpus` plus `llm_generation`, created by `scripts/init-db.sql`, with a non-superuser `rag_app_login` in `rag_app`), `pgbouncer` (transaction mode), `redis` (Valkey-compatible), `qdrant`, `floci` (S3 / SNS / SQS emulator, initialised by `scripts/init-floci.sh`: bucket, two topics, six queues, the probe queue) and `floci-ui`. `make docker-up` waits for health.

### Health and readiness

`/health` and `/live` are liveness; `/ready` is readiness. **Known gap:** `/ready` does not yet reflect the outbox relay (the signal exists as `app.state.relay_ready`, the probe is not wired).

---

## Cross-service dependencies

| Direction | Peer | What | If it is down |
|---|---|---|---|
| Outbound, sync | Model Gateway | `embed`, `rerank`, `draft`, `judge`, `extract`, refiners, tokenize | Retrieval serves lexical-only with a degradation marker; generation returns `503` |
| Outbound, sync | Tender | Domain Profile / Language Pack resolution (Valkey-cached) | Cached pack serves; uncached fails closed |
| Outbound, sync | IAM | Memberships, user LLM context, TAC-4 overlay | TAC-4 failure refuses the request (CG-4); LLM context omitted from the prompt |
| Outbound, sync | Temporal | Start and signal | Async routes `503 WORKFLOW_ENGINE_UNAVAILABLE`; admission rows stay `pending` and a repeat of the same key restarts them |
| Outbound, sync | Qdrant, Valkey, S3 | Vectors, caches, section text | Qdrant: scope store refuses (`503`); Valkey: cache miss; S3: content `503` |
| Inbound | Tender, Ingestion, Worker Pool, operators | Public, C-I, activity writes, W-8 | — |
| Async | SNS / SQS | 17 published, 11 consumed event types | Outbox retries; poison messages dead-letter |

---

## Out of scope

- **Workflow and activity logic, and every LangGraph graph** — the Worker Pool (scope cut S-1). No worker exists yet, so outline, stage and coherence steps, `OutlineReady`, gate expiry and the suspend path are untested end to end.
- **Ingestion's write path** — parsing, chunking, embedding, Qdrant writes, the lexical table (S-2, DEP-5).
- **Model Gateway configuration** — only the HTTP client and adapter live here (S-3).
- **Output content safety** — Tender's, not ours (§15.3).
- **Not built:** the retention sweep (LLD-deferred), the zombie-run and orphan-S3-object sweeps, the corpus-wide conflict check (LD-52, deferred past v1), REGEN-5 reuse, the Glue event encoder (LD-82), scope-cache invalidation on collection writes, tenant purge reaching Valkey keys and evaluation samples.

Full list, with the assumptions about peer contracts: [`docs/implementation-plan.md`](docs/implementation-plan.md).

---

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md). Before opening a PR run `make ci`; if you changed a route, a model or an event payload run `make swag`; and add a `CHANGELOG.md` entry under *Unreleased*.

## License / ownership

Private to BCBP Solutions FZC LLC. Owned by the LLM team (`@BCBP-SOLUTIONS-FZC-LLC/llm-team`, see [`.github/CODEOWNERS`](.github/CODEOWNERS)).
