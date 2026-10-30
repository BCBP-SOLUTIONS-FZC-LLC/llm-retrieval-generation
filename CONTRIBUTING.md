# Contributing

This is an internal LLM-subsystem microservice for the XpertPMS platform. This guide covers development setup, how to extend the service, test requirements, and the PR process for `llm-retrieval-generation`. Read [`README.md`](README.md) for what the service does and [`ARCHITECTURE.md`](ARCHITECTURE.md) for how it is built.

## Prerequisites

- Python 3.15+ (matches `requires-python`; ruff and mypy target 3.15). `make setup` lets `uv` fetch it (macOS currently gets 3.15.0; CI and the image run 3.15.2). `asyncpg` 0.31 (held below 0.32 by `pgcommon` 0.4.1) has no 3.15 wheel, so it compiles on install: you need a C compiler (Xcode command line tools on macOS)
- Docker (required for the compose stack and for the integration and e2e tests, which start `postgres:18` through `testcontainers`)
- `make`. `uv` (pinned `0.13.1`) is installed into `.venv` by `make setup` if it is not on your `PATH` — no separate install needed
- Access to the private BCBP packages `platform-fastapicommon`, `pgcommon` and `eventcommon`. Locally they come from the index your `uv` is configured for; in CI the `CI_REPO_READ_TOKEN` secret provides it (`PY_PRIVATE_INDEX_URL` is not used)

## Development setup

```bash
git clone https://github.com/BCBP-SOLUTIONS-FZC-LLC/llm-retrieval-generation
cd llm-retrieval-generation
make setup      # .venv + uv sync --locked, copies .env-example to .env, installs .githooks/pre-commit
make docker-up  # PostgreSQL x2 DBs + PgBouncer + Valkey + Qdrant + floci (S3/SNS/SQS) + floci-ui
make migrate    # outbox schema, then both numbered migration sets
make lint       # verify the linter passes
make test       # unit + use-case + architecture + API tests (no Docker)
make run        # start the API on :8080
```

To run the full CI set (needs Docker):

```bash
make ci         # lock-check + fmt-check + lint + typecheck + rls-check + swag-check + test-ci + build
make test-e2e   # real app over a real RLS Postgres
```

`make setup` installs `.githooks/pre-commit`, which runs `fmt-check`, `lint`, `typecheck` and `swag-check` (the generated specs are not stale) before every commit, and `lock-check` when `pyproject.toml` or `uv.lock` is staged. `.githooks/pre-push` builds the image and runs `make trivy` (the release CVE gate) before every push; it needs Docker, skips branch deletions, and `git push --no-verify` bypasses it. Re-run `make install-hooks` any time a file in `.githooks/` changes.

Host ports for the local stack are deliberately offset from the IAM services' stacks so both can run side by side: direct Postgres `5535`, PgBouncer `5536`, Valkey `6381`, Qdrant `6335`, floci `4568`, floci-ui `4501`. Container-internal ports are unchanged.

## Project layout

```
src/rag/
  domain/          entities, value objects, enums, domain services, repository ports — stdlib only
  application/     commands, queries, DTOs, use cases, outbound ports, event contracts
  infrastructure/  persistence, gateway, tender, iam, temporal, cache, storage, events, sweeps, observability, config.py
  presentation/    api/{public,internal,operator}/, dependencies, errors, identity, api_docs, main.py
  corpusscope/     scope resolution + every Qdrant filter (vendored library)
migrations/{corpus,generation}/   NNNN_name.up.sql + .down.sql, one set per database
schemas/events/                   JSON Schemas derived from the event contracts
api/asyncapi.{yaml,html}          generated event contract and catalogue page
docs/swagger/                     generated OpenAPI + the Swagger UI theme
scripts/                          gen_api_docs.py, asyncapi_page.*, init-db.sql, init-floci.sh
tests/{unit,usecase,repository,api,events,architecture,isolation,integration,e2e}/
```

Each layer holds one subpackage per LLD module (`corpus`, `retrieval`, `generation`, `verification`, `workflows`, `packs`). The layer rules are enforced by `tests/architecture/test_dependency_rules.py`: `domain` imports the standard library only, `application` imports `domain` (and, from the platform, only `fastapicommon.logging`), and `infrastructure` / `presentation` point inward. Only `corpusscope` may import `qdrant_client`.

## Extending the service

### Adding a new domain entity or value object

1. Add the type to `src/rag/domain/<module>/` — standard library imports only (the architecture test fails otherwise).
2. Put construction and validation on the type itself (a `create` classmethod or `__post_init__`); each aggregate validates its own invariants.
3. Add unit tests in `tests/unit/` (e.g. `tests/unit/test_generation_domain.py`).

### Adding a new use case

1. If it needs a new repository or outbound capability, add the port (a `Protocol`) in `domain/<module>/repositories.py` or `application/<module>/ports.py`.
2. Implement it in `src/rag/application/<module>/`. **Every state change and its event share one transaction** (`TransactionRunner.run_in_tx` with `EventPublisher.publish(tx, event)`, EVT-8) — no dual writes, no "publish first, commit later".
3. Implement the port in `src/rag/infrastructure/`, wire it in `presentation/*_dependencies.py`, and add the route (below).
4. Add use-case tests in `tests/usecase/` against the in-memory fakes in `tests/support_*.py`; add repository coverage in `tests/repository/` if it involves SQL.

### Adding a new HTTP endpoint

1. Add the route in `src/rag/presentation/api/{public,internal,operator}/`. Every route has a stable LLD ID (`R-`, `A-`, `W-`, `P-`, `C-I`, `C-M`); **never reuse a retired one** (`C-T1`, `C-T2`) and say which ID you implement in the docstring.
2. Declare the identity dependency (`require_identity` for public routes, `require_tenant` for internal ones, the role dependency where the LLD names a role). `tests/architecture` and the CI-1 route scan fail a route with no auth dependency; the one named exemption is C-M1.
3. Resolve scope **before** any database read of tenant data, and answer absent and ungranted resources identically (`404`, CG-8).
4. Put request/response models beside the router (`*_models.py`) and map domain errors to statuses in `presentation/errors.py`, not in the route.
5. Run `make swag` and commit the regenerated `docs/swagger/*` and `api/*` alongside your change — `make swag-check` and `tests/unit/test_api_docs.py` fail a PR whose code diverges from them. Update the API overview table in `README.md`.

### Adding a new migration

1. Add `NNNN_description.up.sql` and `NNNN_description.down.sql` to `migrations/corpus/` or `migrations/generation/`. **Numbering is per instance and monotonic** — never reuse a number or reorder existing ones; every `.up.sql` has a `.down.sql` that reverses it. See [`migrations/README.md`](migrations/README.md).
2. Open with `SET ROLE rag_owner;` and finish with `RESET ROLE;` so every object is owned by `rag_owner`, which `FORCE ROW LEVEL SECURITY` then binds.
3. A new tenant table gets `tenant_id`, the uniform `tenant_isolation` policy, `ENABLE` **and** `FORCE ROW LEVEL SECURITY`, and per-table grants for the roles that need them (`rag_app`, `rag_activity`, `rag_sweeper`). Only `processed_events` and the library's `outbox_*` tables are exempt.
4. **Mutable tables carry `record_version`, `created_at` and `updated_at`, maintained by trigger (LD-103).** Never set `record_version` from application code, and every versioned `UPDATE` includes `WHERE id = $1 AND record_version = $2`. Append-only tables carry neither and use `reject_always`.
5. Primary keys default to `uuidv7()` (PostgreSQL 18).
6. **Additive first, destructive later.** Ship column drops and type narrowing in a release after the last read or write of the object, so the previous image still works while a deploy rolls out. A migration that is not safe under the previous image is a MAJOR change ([`VERSIONING.md`](VERSIONING.md)).
7. Verify with `make docker-up && make migrate`, then `make test-integration`. Add or update the RLS tests in `tests/repository/` for any tenant-scoped table.

The outbox schema is applied **before** the numbered sets (`migrate_all`) because `0003` grants on tables the library owns — never apply `migrations/` by hand without it.

### Adding a new domain event type

1. Add the payload dataclass and the type constant to `src/rag/application/events/contracts.py` (standard library only — the architecture test allows no third-party import there), and register it in `PUBLISHED` (or `CONSUMED`) and in the generation or index topic set.
2. Build the event in the use case with `DomainEvent` and publish it through `EventPublisher.publish(tx, event)` inside the same transaction as the state change. An unregistered type or an off-schema payload raises inside that transaction (EVT-9).
3. Run `make swag`: it runs `make extract-schemas` (deriving the JSON Schema under `schemas/events/` from the contract) and regenerates `api/asyncapi.yaml` and the catalogue page; `make swag-check` fails on a stale schema file. Commit all of it. Every event object allows additional properties (LD-65), so additive fields are not breaking; renaming or removing a field is.
4. Never put `tenant_id` inside `data` — the envelope's `tenant_id` is authoritative.
5. For a **consumed** event, add the handler in `infrastructure/events/consumers.py` and the use case in `application/events/consumers.py`; a consumer binds the tenant, runs one use case and writes the ledger row, and sends poison (non-UUID tenant, schema violation, forged event) to the queue's DLQ.
6. Update the event lists in `README.md` and, for a new consumer queue, `scripts/init-floci.sh` and `.env-example`.
7. Run `make schema-validate` (Docker, `platform-schemagov`) before pushing.

### Adding a new environment variable

1. Add a typed field to `Settings` in `src/rag/infrastructure/config.py`. The variable name is the upper-cased field name; LLD §17 variables keep their exact names and defaults (and are listed in `tests/unit/test_config.py`).
2. Add it to `.env-example` with an inline comment explaining purpose and accepted values.
3. Update the Environment variables table in `README.md`; a variable that changes the runtime contract also goes through [`VERSIONING.md`](VERSIONING.md).
4. Give it a range or format check (`PosInt`, `PosFloat`, `Fraction`, `Unit` in `config.py`) and, if it must agree with another setting, a rule in the `_consistent` model validator. A bad value must stop the process at startup, and `tests/unit/test_config.py` has a case for each.
5. A setting that would silently weaken a control if misspelled stays a string and is asserted at startup (as `PG_BOUNCER_MODE` is, via `assert_isolation_config`).

## Testing requirements

| Layer | Location | Docker | Notes |
|-------|----------|--------|-------|
| Unit | `tests/unit/` | No | Fully isolated; fakes and `httpx.MockTransport` for peers |
| Use case | `tests/usecase/` | No | Use cases over the in-memory fakes in `tests/support_*.py`, which emulate the unique indexes and the version trigger |
| API | `tests/api/` | No | The real app with dependency overrides; routes, status mapping, SSE contract |
| Events, isolation, architecture | `tests/events/`, `tests/isolation/`, `tests/architecture/` | No | Consumers and relay on fakes; scope isolation; layering and DEP rules |
| Repository / migrations | `tests/repository/`, `tests/integration/` (`-m integration`) | Yes | Real Postgres 18 via testcontainers; RLS, triggers, grants, concurrency |
| E2E | `tests/e2e/` (`-m e2e`) | Yes | Real app over real RLS Postgres, non-superuser login in `rag_app`: flows, security, error shape, infra |

`make test` runs everything that needs no Docker. Integration and e2e tests are opt-in by marker and skip cleanly when Docker is unavailable; set `RAG_TEST_PG_DSN_BASE` to point them at an existing Postgres instead.

### Canonical test cases

Do not weaken or delete these without a design discussion:

- `tests/architecture/test_dependency_rules.py` — layering and DEP-1..4 (Qdrant importer, gateway-only HTTP, `corpus` as a leaf, `verification` never importing `retrieval`).
- `tests/unit/test_error_taxonomy.py` — the 32 error codes and their statuses.
- `tests/unit/test_api_docs.py` — committed specs equal generated output.
- `tests/unit/test_config.py` — every LLD §17 variable and default; the PgBouncer-mode startup refusal (ISO-11).
- `tests/repository/*` and `tests/e2e/test_security.py` — cross-tenant isolation under `FORCE` RLS, including the PgBouncer pooled-backend case (RLS-6).
- `tests/unit/test_events_runtime.py` — six queues, Glue decoding on the two IAM queues only, poison to DLQ at the fourth receive.

### Coverage gate

`make test-ci` runs unit and integration together and writes `coverage.xml`. CI fails below **95%** (`.github/scripts/coverage-gate.sh`, `COVERAGE_THRESHOLD`); current coverage is about 97.8%. A new module needs tests that keep it above the gate on its own — do not rely on the rest of the suite to carry it.

### Testcontainers note

Integration and e2e tests start `postgres:18` with testcontainers. On macOS Docker Desktop the Makefile points `DOCKER_HOST` at the user socket. In CI the Ryuk reaper is disabled for the e2e step (`TESTCONTAINERS_RYUK_DISABLED`).

### PgBouncer and RLS note

Superusers bypass row-level security even with `FORCE`, so tests that assert isolation connect as a non-superuser in `rag_app`. Production DSNs go through PgBouncer in **transaction mode**; the tenant GUC must therefore only ever be set transaction-locally (see Guardrails).

## Linting

```bash
make lint        # ruff check + ruff format --check
make typecheck   # mypy --strict on src
make fmt         # ruff --fix and format
```

`ruff` selects `E F I UP B D ANN` (line length 100): every public function in `src/` has a type annotation and a docstring. Tests are exempt from `D` and `ANN`. `mypy --strict` runs on `src` only.

## Guardrails and forbidden patterns

- **Do not commit `.env`.** It is in `.gitignore`; `.env-example` is the only committed template.
- **Reach the database only through `pgcommon`.** Never import `asyncpg`, `psycopg`, `sqlalchemy` or any other driver or ORM; open pools only with `Pool.create(pool_config(...))` from `infrastructure/persistence/pools.py` (the one place a `Config` is built, so tuning, PgBouncer mode and the tenant GUC provider cannot drift per pool); run every statement inside `run_in_tx` (our `PgTransactionRunner`); classify errors with `pgcommon.is_unique_violation`, `constraint_name`, `is_connection_exception` and friends, never by driver exception type. `pgcommon` is imported by `infrastructure` and `presentation` only. Enforced by `tests/architecture/test_database_access_only_through_pgcommon.py`.
- **Reach events only through `eventcommon`.** No raw SNS/SQS client (boto3 is S3's), no SQL on `outbox_events`, `outbox_dead_letters` or `processed_events` (the library creates and owns them; a migration here only grants), every envelope from `new_envelope`. Relay, publisher and consumer configurations are built only in `infrastructure/events/config.py` from `eventcommon.config`; tuning is the library's `OUTBOX_*` / `SQS_*` environment, durations with a unit (`5s`). Enforced by `tests/architecture/test_events_only_through_eventcommon.py`.
- **Never write a session-scoped `SET app.tenant_id`.** The tenant GUC is bound transaction-locally by the pool's GUC provider (`request_guc_provider`, or `bind_tenant_scope` outside a request). A session-scoped `SET` survives into the next PgBouncer client — the cross-tenant breach RLS-6 exists to prevent. CI runs `make rls-check` (a grep of `src/` and `migrations/`) and fails the build; run it locally too. Use `SET LOCAL` or `set_config(..., true)`.
- **Never bind a tenant from a string.** `bind_tenant_scope` takes a `UUID`; `'system'` or any non-UUID must never reach a `::uuid` policy (LD-19).
- **Never import `qdrant_client` outside `corpusscope`**, and never build a Qdrant filter outside it. The mandatory compound filter and `is_current` are unrelaxable (DEP-1).
- **Never build an HTTP client toward an inference backend outside `infrastructure/gateway`**, and never read `gateway_base_url` elsewhere (DEP-2). This service holds no provider credential.
- **Never import `retrieval`, `generation` or `verification` from `corpus`** (DEP-3), or `retrieval` from `verification` (DEP-4). `preferred_model` is a hint — do not pre-filter it.
- **Never write S3 outside `tenants/<tenant>/…`.** The store refuses a key outside the prefix; tender purge and offboarding delete by prefix (ART-9).
- **Optimistic locking:** every versioned `UPDATE` includes `WHERE id=$1 AND record_version=$2`; the trigger owns the column.
- **Every state change and its event share one transaction.** No dual writes (EVT-8).
- **Log only with `get_logger`.** `from fastapicommon.logging import get_logger; log = get_logger(__name__)`, then structured calls: `log.warning("run_close_failed", error=type(exc).__name__)` — an event name plus fields, never a formatted sentence. Never `import logging`, `getLogger` or `basicConfig`; never import `prometheus_client`, `opentelemetry` or `structlog`. Metrics and tracing are built from `fastapicommon.metrics` / `fastapicommon.telemetry` in `infrastructure/observability` only. `tests/architecture/test_shared_libs_only.py` enforces all of this. To assert on a log in a test, patch the module's `log` with `tests/support_logs.RecordingLog`.
- **Never log or label content.** No query text, chunk text, refinement content or ids as message text, and metric labels are bounded vocabularies — never a tenant, user or id (OBS-2).
- **Fail closed.** A missing scope store, IAM answer or workflow engine is a specific `503`, never a default. Do not add a code path that answers with a guess.
- **Never hand-edit generated files** (`docs/swagger/swagger.*`, `api/asyncapi.*`, `schemas/events/*.json`). Change the code and run `make swag`.
- **Never add a dependency without a lockfile update.** Run `make lock` and commit `uv.lock`; CI runs `make lock-check`.
- **Do not add `# noqa` or `# type: ignore` without a reason on the same line.**

## Documentation update checklist

When your change touches a public contract or an internal data flow, update the following:

| What changed | Documents to update |
|---|---|
| New HTTP endpoint | The route · `make swag` (commit `docs/swagger/` and `api/`) · `README.md` API overview table · `.claude/api-caching-events.md` |
| New event type (published or consumed) | `contracts.py` · `make swag` (commit `schemas/events/`, `api/`) · `README.md` event lists · `.claude/api-caching-events.md` · `scripts/init-floci.sh` for a new queue |
| New environment variable | `Settings` · `.env-example` (inline comment) · `README.md` Environment variables table · `.claude/operations.md` |
| New migration | `CHANGELOG.md` *Unreleased* with a short schema-change note · `migrations/README.md` table · `.claude/database-schema.md` if a table is added |
| Changed request or write flow | `ARCHITECTURE.md` flow diagram · `.claude/request-flows.md` if the invariant catalogue changes |
| New port interface or peer client | `ARCHITECTURE.md` ports section · `.claude/operations.md` integration points · `docs/implementation-plan.md` assumed-contracts list |
| New sweep | `README.md` · `.claude/operations.md` · `docs/observability/runbooks.md` if it alerts |
| Release, image or workflow change | `VERSIONING.md` (including its *Planned* table) · `README.md` CI section |
| Anything an operator must react to | `docs/observability/runbooks.md` and a `docs/runbook-*.md` where it needs steps |

## Branch naming

Feature branches follow one of these forms:

- `feat/<short-topic>` — new capability
- `fix/<short-topic>` — bug fix
- `docs/<short-topic>` — documentation-only change
- `chore/<short-topic>` — dependencies, tooling, CI, refactors with no behavioural change

Base new branches off the latest `main`. Long-running integration branches are used only for coordinated multi-service work — check with the maintainers before targeting anything other than `main`.

## Pull request checklist

- [ ] `make ci` passes locally — `lock-check`, `fmt-check`, `lint`, `typecheck`, `rls-check`, `swag-check`, `test-ci` and `build`, mirroring CI
- [ ] `make cover-check` if you added or removed code in a thinly covered module (the gate is 95%)
- [ ] `make test-e2e` passes if you touched a route, middleware, migration or the app factory
- [ ] New public functions have type annotations and docstrings
- [ ] New behaviour is covered by tests in the appropriate layer, and coverage stays above the 95% gate
- [ ] Route, model or event changes: `make swag` run and the regenerated files committed (CI runs `make swag-check`)
- [ ] `CHANGELOG.md` *Unreleased* updated (`.github/workflows/changelog-check.yml` fails PRs that touch `src/`, `migrations/` or `schemas/` without an entry)
- [ ] `uv.lock` updated if dependencies changed (`make lock-check`)
- [ ] Documentation update checklist above reviewed — relevant docs updated
- [ ] No new cross-tenant data path — RLS tests added or updated for any tenant-scoped table
- [ ] No session-scoped `SET app.tenant_id` introduced (CI grep blocks it)
- [ ] Contract changes called out in the PR description (route, event `type` or field, error code, env var, table the Worker Pool writes) so [`VERSIONING.md`](VERSIONING.md)'s MAJOR/MINOR rules are applied

## Commit style

Keep messages concise; describe the *why*. Reference the LLD section or invariant ID when it clarifies the change:

```
Add COL-9 slot-conflict handling to collection insert

A concurrent first document can trip uq_coll_tender before ON CONFLICT (id)
arbitrates. Insert with DO NOTHING plus an existence check; a real conflict
under a different id still raises. (LLD §4.7.1, LD-33.)
```

Use a short imperative subject line (≤ 72 chars). Reference the area:

```
fix(corpus): resolve a concurrent C-I1 race without raising uq_coll_tender (COL-9)
feat(workflows): add W-7 resume for runs suspended on quota (ASYNC-11)
feat(consumer): add EVT-14 recency guard on the tenant projection
docs: update Environment variables table for SQS_MAX_RECEIVE_COUNT
test(rls): add pooled-backend cross-tenant GUC leak assertion
fix(events): fail closed on an unregistered event type at enqueue (EVT-9)
```

## Deployment

**Partly planned** — a `Dockerfile` exists (`make docker-build`; base images are digest-pinned, `make pin-base-images` updates them and `.docker-digests`), and a Helm chart exists in `deploy/helm/` (run `make helm-check` after any change there; CI does), and `.github/workflows/release.yml` builds, scans, boot-tests, pushes and signs the image on a version tag (never run yet; see [`VERSIONING.md`](VERSIONING.md#planned-not-yet-in-place)). CI validates and boot-tests the image; it publishes nothing.

**On push to `main` and on pull requests** (`.github/workflows/ci.yml`, skipped for Markdown-only changes):

1. `validate-test` runs `make test-ci` (unit + integration), the 95% coverage gate, then `make test-e2e`.
2. `validate-quality` runs the lockfile check, `uv sync --locked`, `uv pip check`, the RLS-6 grep, format, lint, `mypy --strict` and `pip-audit`, in parallel with `validate-test`.
3. `build-image` (`Build image (cache)`), then `trivy` (`Trivy CVE scan`, CRITICAL/HIGH with a fix fails) and `smoke` (`Smoke tests`: migrate, start, `/ready` = 200 against Postgres + PgBouncer).
4. `pr-summary` posts the results on pull requests.

The organisation ruleset on `main` requires these checks plus a code-owner approval; the job names above match it. Run `make trivy` locally (the pre-push hook does) to catch a CVE failure before pushing.

**Planned, with the first image:** build, CVE scan and smoke test, push to GHCR with a Cosign keyless signature, and a tag-triggered release with SBOM and provenance. Migrations will run from a one-shot job directly against Postgres before the new image takes traffic. Notify the platform team if a migration, a breaking env-var change or a new event consumer needs coordinated rollout.
