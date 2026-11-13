# CLAUDE.md

This file provides guidance to Claude Code when working with the **Retrieval & Generation Service** (`llm-retrieval-generation`), a Python/FastAPI microservice in the LLM subsystem (Tender Management SaaS platform). It implements the LLD at `docs/lld/llm-lld-retrieval-generation-service.md` (6,673 lines); where the LLD and the LLM HLD disagree, the HLD is authoritative. Build status, per-slice deviations, assumed peer contracts and the traceability matrix are in `docs/implementation-plan.md` — read it before changing behaviour the LLD describes.

The subsystem has four planned deployables: Ingestion, Retrieval & Generation (this repo), Worker Pool and Model Gateway (Agent Router, formerly Envoy AI Gateway). **Only this one exists here.** Workflow and activity logic, including every LangGraph graph, is the Worker Pool's (scope cut S-1); the Gateway is Agent Router (formerly Envoy AI Gateway, renamed September 2026), whose route configuration is not in this repo.

## What This Repo Is

A **private Python service** (`github.com/BCBP-SOLUTIONS-FZC-LLC/llm-retrieval-generation`, Python 3.15+, FastAPI) that owns the **retrieval and generation layer**: which collections a caller may search, hybrid retrieval, grounded answers and drafts, structured extraction, long-form runs (started and signalled, not executed), verification, and the audit record of all of it.

**Key responsibilities (sole writer for, except where the Worker Pool's activities write under `rag_activity`):**
- **Collections** (`collections`, `collection_grants`, `index_generations`, `retention_policies`, `tenant_lifecycle` in `llm_corpus`) — placement, overlay answers, activation, retention, freeze and purge.
- **The generation record** (`llm_generation`) — artefacts, versions, sections, section versions, `generation_runs`.
- **The retrieval record** — `retrieval_sets`, `retrieval_set_chunks`, refinements.
- **Verification** — `groundedness_reports`, `claim_verifications`, `completeness_checks`, facts and conflicts.
- **The audit chain** (`artefact_audit_chain`), the inbox ledger (`processed_events`), and the outbox on both databases.
- **Scope resolution** (`corpusscope`) — the one place that decides what a caller may read, and the only importer of `qdrant_client`.

**Does NOT own:** documents, chunks, vectors, the lexical index (Ingestion); workflow/activity logic and LangGraph graphs (Worker Pool); model routing, credentials, residency, quota (Model Gateway); users, tenants, roles (IAM); Domain Profiles and Language Packs (Tender); output content safety (Tender, §15.3).

## Common Commands

```bash
make setup            # .venv + uv sync --locked, copy .env-example to .env, install git hooks
make lock             # uv lock
make lock-check       # fail if uv.lock is stale vs pyproject.toml
make fmt              # ruff --fix + ruff format
make fmt-check        # ruff format --check (mirrors CI)
make lint             # ruff check + ruff format --check
make typecheck        # mypy --strict on src
make test             # unit + use-case + architecture + API tests (no Docker)
make test-ci          # unit + integration with coverage (CI; needs Docker)
make test-unit        # unit-ish directories only
make test-integration # migration / Postgres tests (needs Docker)
make test-e2e         # real app over a real RLS Postgres (needs Docker)
make extract-schemas  # derive schemas/events/*.json from the event contracts
make swag             # extract-schemas + regenerate docs/swagger/*, api/asyncapi.*
make swag-check       # fail if the generated specs or event schemas are stale (drift gate)
make rls-check        # fail on a non-LOCAL SET app.tenant_id (RLS-6; CI calls it)
make cover-check      # test-ci, then the coverage gate (95%)
make build            # uv build into dist/
make run              # API on :8080 (creates .env if missing, kills port 8080 first)
make sweeps           # run the sweep runner locally
make migrate          # outbox schema, then both numbered migration sets
make ci               # lock-check + fmt-check + lint + typecheck + rls-check + swag-check + test-ci + build
make docker-up        # postgres x2 + pgbouncer + valkey + qdrant + floci + floci-ui (http://localhost:4501)
make docker-down      # stop containers
make events-peek      # print events waiting in the dev probe queue (does not consume)
make gateway-up       # Agent Router (pinned image) + model stub, opt-in; :1975
make gateway-down
make gateway-up-vllm  # embed + rerank on vLLM (CPU) behind the gateway, to test the self-hosted contract
make vllm-down        # stop vLLM, gateway back on the stub
make test-gateway     # contract tests of our client against it (needs gateway-up)
make vuln-check       # pip-audit on the locked deps
make trivy            # build the image, then Trivy CVE scan as release.yml does (Docker)
make schema-validate  # platform-schemagov over schemas/events (Docker, linux/amd64)
make clean
```

Single test: `.venv/bin/python -m pytest tests/unit/test_config.py::test_env_overrides_by_upper_case_name -q`. Integration and e2e tests are opt-in by marker (`-m integration`, `-m e2e`); the default `pytest` run excludes both.

**Testcontainers note:** integration and e2e tests start `postgres:18`. Docker must be running; set `RAG_TEST_PG_DSN_BASE` to use an existing server. Superusers bypass RLS even with `FORCE`, so isolation tests log in as a non-superuser in `rag_app`.

## Architecture

Clean Architecture — dependencies point inward, enforced by `tests/architecture/test_dependency_rules.py`. `domain` (stdlib only) ← `application` ← `infrastructure` / `presentation`, with six LLD modules (`corpus`, `retrieval`, `generation`, `verification`, `workflows`, `packs`) and the vendored `corpusscope` library. DEP-1..4: only `corpusscope` imports `qdrant_client`; no HTTP client toward an inference backend outside `infrastructure/gateway`; `corpus` is a leaf; `verification` never imports `retrieval`. Full detail with diagrams: [`ARCHITECTURE.md`](../ARCHITECTURE.md) and [`architecture.md`](architecture.md).

## Key Files to Know

Full per-file descriptions in **[`key-files.md`](key-files.md)**. Most-touched entrypoints:

- `src/rag/presentation/main.py` — app factory, lifespan (pools, container, relays and consumers), router registration
- `src/rag/presentation/dependencies.py` — `Container`, `build_container`; `*_dependencies.py` compose each module
- `src/rag/application/generation/{flow,generate}.py` — admission, run lifecycle, completion, the short-form engine
- `src/rag/application/retrieval/pipeline.py` — the retrieval pipeline
- `src/rag/corpusscope/resolver.py` — scope resolution
- `src/rag/infrastructure/events/{publisher,routing,relay,consumers,runtime}.py` — outbox, relays, consumers
- `src/rag/infrastructure/persistence/{pools,migrate}.py` — pools with the tenant GUC provider; migration wiring
- `scripts/gen_api_docs.py` — generates every committed spec

## Claude Code automation in this repo

- **Hooks** (`.claude/settings.json`): `format-python.sh` runs `ruff format` on edited project `.py` files; `block-generated.sh` refuses edits to generated specs (run `make swag`). Both need `jq`.
- **Skills**: `/pr-check` runs the pre-PR gates; `peer-contract-check` (background) means cite the peer's doc or record an assumption.
- **Agents** (read-only): `security-reviewer`, `rls-reviewer`. Use them on changes to scope, identity, IAM clients, migrations or repositories.
- **Git hooks**: `.githooks/pre-commit` and `pre-push` (`make trivy`, Docker; `--no-verify` bypasses). Install with `make install-hooks`.
- **Branching**: feature branch and PR into `main` (default; org ruleset: code-owner approval and the six `ci.yml` checks). CI has not completed on GitHub yet: Actions jobs did not start because of an account billing limit.

## Reference docs

| Doc | Contents |
|---|---|
| [`architecture.md`](architecture.md) | Directory tree, layer rules, ports, shared libraries |
| [`key-files.md`](key-files.md) | Critical files with invariants and caveats |
| [`database-schema.md`](database-schema.md) | Both databases: tables, roles, RLS, triggers, migrations |
| [`api-caching-events.md`](api-caching-events.md) | Routes, caching, events |
| [`request-flows.md`](request-flows.md) | Request, stream, workflow and consumer flows; concurrency; erasure |
| [`operations.md`](operations.md) | Security, configuration, sweeps, observability, deployment, testing |

## Conventions That Bite

- **Reach the database only through `pgcommon`**: no driver imports; pools only via `pool_config` in `persistence/pools.py`; statements inside `run_in_tx`; errors classified with `pgcommon` helpers (`is_unique_violation`, `constraint_name`). Enforced by `tests/architecture/test_database_access_only_through_pgcommon.py`.
- **Never write a session-scoped `SET app.tenant_id`.** The GUC is transaction-local, set by the pool's provider; CI greps for violations (RLS-6). `PG_BOUNCER_MODE` must be exactly the string `true`.
- **A state change and its event share one transaction** (`run_in_tx` + `EventPublisher.publish(tx, …)`). Never publish from the request path.
- **Fail closed.** A dependency that cannot answer is a specific `503`, never a default. An empty scope is an answer; a failed read of the scope store is not.
- **Absent and ungranted are the same `404`** (CG-8).
- **Never hand-edit generated files**: `docs/swagger/swagger.*`, `api/asyncapi.*`, `schemas/events/*.json`. Change the code and run `make swag`.
- **Never reuse a retired route or LLD ID** (`C-T1`, `C-T2`).
- **Reach events only through `eventcommon`**: no raw SNS/SQS client, no SQL on `outbox_events`/`processed_events` (the library owns them), envelopes via `new_envelope`; relay/publisher/consumer configs only from `infrastructure/events/config.py`; tuning is the library's `OUTBOX_*`/`SQS_*` env (durations with a unit, e.g. `5s`). Enforced by `tests/architecture/test_events_only_through_eventcommon.py`.
- **Log only with `fastapicommon.logging.get_logger`** (structured: event name plus fields). Never `import logging`, `prometheus_client`, `opentelemetry` or `structlog`; metrics and tracing live in `infrastructure/observability`. Enforced by `tests/architecture/test_shared_libs_only.py`.
- **Never log or label content** (OBS-2).
- **Migrations are per instance, monotonic, and each has a `.down.sql`**; they open with `SET ROLE rag_owner` and apply the outbox schema before the numbered sets.
- **Peer contracts are assumptions** (Gateway routes, Tender pack JSON, IAM bodies): do not harden a guess into a "fact" — record it in `docs/implementation-plan.md`.
- **Mark unbuilt things as planned.** `VERSIONING.md` has a *Planned* table; keep it honest rather than describing missing machinery as present.
