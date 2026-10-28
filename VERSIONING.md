# Versioning and releases

This repository is a **deployed Python (FastAPI) microservice**, not a library other services `pip install` — the `rag` package is internal to this process, and `pyproject.toml`'s `[project]` metadata exists so this repo builds and locks, not for import by sibling repos. What gets versioned here is the **container image** (`ghcr.io/bcbp-solutions-fzc-llc/llm-retrieval-generation`, planned — see [Planned (not yet in place)](#planned-not-yet-in-place)) plus the runtime contract Tender, Ingestion, IAM, the Worker Pool, the Model Gateway, and operators depend on. Versions are published with **Git tags** and described in [CHANGELOG.md](./CHANGELOG.md).

This document follows the same SemVer / image-tag / maintainer-process layout as [`iam-org-membership/VERSIONING.md`](https://github.com/BCBP-SOLUTIONS-FZC-LLC/iam-org-membership/blob/main/VERSIONING.md). Where that service has a mechanism this one does not have yet (Dockerfile, `release.yml`, Helm chart, Cosign signing), the section says so instead of describing it as if it existed. Every such item is marked **Planned** where it appears and tracked in one table in [Planned (not yet in place)](#planned-not-yet-in-place).

## Semantic versioning (SemVer)

We use [SemVer 2.0.0](https://semver.org/): `MAJOR.MINOR.PATCH` (e.g. `v1.2.3`).

| Bump | When you change | Examples |
|------|-----------------|----------|
| **MAJOR** | Breaking change in the runtime contract (§ below) | Removing/renaming a route (R-/A-/W-/P-/C-I/C-M ID), a published event `type` or its payload field names, a consumed event's expected shape, a processed-events `consumer` name, a required env var, a domain error **code**, a database migration that is not backward compatible with the previous image |
| **MINOR** | New backward-compatible capability | A new endpoint (new ID, never reuse a retired one — see "Retired and reserved IDs" below), implementing a reserved route (A-8, C-I3, C-I9), a new optional env var, a new published or consumed event type, a new `llm_retrieval_generation_*` metric, an additive payload field, an additive migration |
| **PATCH** | Backward-compatible fix | Bug fix, performance improvement, dependency bump with no observable behavior change, documentation-only correction |

### What counts as the runtime contract

This service has no package for another service to import — its "public API" is the wire/deployment contract every caller and operator depends on. The closest equivalents of a frozen-name registry are the committed specs generated from code (`docs/swagger/swagger.json`, `api/asyncapi.yaml`, `schemas/events/*.json`), the event contracts in `src/rag/application/events/contracts.py`, and `src/rag/domain/shared/errors.py`. `make swag-check` and `tests/unit/test_api_docs.py` fail when any of them drifts from the code, so a contract change is always visible in a diff.

| In scope (SemVer applies) | Out of scope (may change without MAJOR) |
|---------------------------|------------------------------------------|
| The 35 routes — `/api/v1/*` public (R-1..7, A-1..9, W-1..7, P-1/2/3/6; 27), `/api/v1/internal/collections/*` (C-I1, C-I2, C-I4, C-I5, C-I6, C-I8; 6), `/api/v1/operator/*` (W-8; 1), `/api/internal/models` (C-M1; 1) — method, path, and request/response field names (`docs/swagger/swagger.json`) | `rag.*` package structure, module and class names, file layout — nothing here is imported by another service. FastAPI's internal router structure is not part of the wire contract |
| The 17 published event `type` names and their payload fields — 10 on `llm-generation-events`, 7 on `llm-index-events` (`contracts.py`, `schemas/events/`) — and the 11 consumed event types on the 6 queues (`llm-document-rag-q`, `domainprofile-llm-invalidate-q`, `iam-rag-purge-q`, `iam-rag-membership-q`, `tender-rag-q`, `usage-rag-q`) | Envelope field order / JSON key order; the Glue wire-format header internals; additive payload fields (every event schema allows additional properties) |
| The processed-events ledger names, one per consumer (`runtime.QUEUES`), and the SSE frame shapes of R-2/R-3/A-6 (draft frames, the terminal verdict frame, the terminal `error` frame) | Internal dedup implementation details (`eventcommon` inbox); SSE heartbeat comment lines |
| Domain error **codes** (`ErrorCode`, 32 codes, e.g. `ARTEFACT_NOT_FOUND`, `OPTIMISTIC_LOCK_CONFLICT`, `SCOPE_STORE_UNAVAILABLE`) and their HTTP statuses (`presentation/errors.py`, LLD §24); the RFC 9457 problem shape | Exact error `message` wording |
| Required env var **names and semantics** (`.env-example`; required outside dev: `CORPUS_DATABASE_URL`, `GENERATION_DATABASE_URL`, `CORPUS_MIGRATION_DATABASE_URL`, `GENERATION_MIGRATION_DATABASE_URL`, `PG_BOUNCER_MODE` (exactly `true`), `VALKEY_URL`, `QDRANT_URL`; with `EVENTS_ENABLED=true`: `GENERATION_TOPIC_ARN`, `INDEX_TOPIC_ARN` and the queue URLs; sweeps: `CORPUS_SWEEP_DATABASE_URL`, `GENERATION_SWEEP_DATABASE_URL`) | Env var **defaults** (the LLD §17 design defaults such as `RRF_K=60`, `IDEMPOTENCY_TTL_SECONDS=3600`) — tunable without a MAJOR bump unless the new default itself breaks a documented invariant |
| Database schema as the Worker Pool and Ingestion see it: table and column names in `llm_generation` / `llm_corpus` they read or write under `rag_activity` / `rag_sweeper` / their own roles (`migrations/`), and the role names (`rag_app`, `rag_activity`, `rag_owner`, `rag_sweeper`) | Tables and columns only this service touches; index names; trigger bodies |
| Prometheus metric **names** (`llm_retrieval_generation_*`, Tier 3; `OBS-3` naming rules) — dashboards and alerts key off them. Metrics are off until the `llm` domain is registered in the platform registry (OQ-103); the names are frozen from the day they are enabled | Label cardinality beyond what is documented, histogram bucket boundaries; `platform_*` names owned by `fastapicommon` / `eventcommon` / `pgcommon` |
| `GET /health`, `/live`, `/ready` — existence and meaning | `GET /`, `/docs`, `/openapi.json`, `/swagger/*`, `/asyncapi`, `/asyncapi.yaml` — documentation surfaces, not a contract a caller must hold stable (the raw specs are, as listed above) |

### Retired and reserved IDs

- **Retired** (permanently unregistered, never reused): `C-T1`, `C-T2` (retired by LD-41; Tender calls no provisioning route — `C-I1` resolves or creates).
- **Reserved** (the ID exists, the route does not): `C-I3`, `C-I9` (shapes deferred, LD-66). `A-8` is mounted but answers `503 RENDERER_UNAVAILABLE` (scope cut S-6); implementing it is a MINOR change.
- Not routes of this service, so not registered here: `C-I7`, `C-I10` (required *of* Ingestion).

Reusing a retired ID would itself be a breaking, confusing change even though the ID is technically "free."

This service has no direct dependency on Temporal server, Qdrant, or the Model Gateway as a *client library version* it promises to callers — those are peers tracked by their own repos' releases. What it does promise is the set of calls it makes (the Gateway task classes, the assumed Tender/IAM contracts in `docs/implementation-plan.md`), which is recorded in the LLD rather than here.

### Guarantees

- **Pre-`v1.0.0` (current status):** per [SemVer §4](https://semver.org/#spec-item-4), anything may change at any time while the major version is `0` — this service has never been deployed to any environment and has not yet committed to a stable *service* contract via a Git tag. The table above still indicates what is *more* disruptive than what within `v0.x` (a MINOR bump is still meant to signal "safer than a MAJOR bump would have been"), but a downstream caller should not yet assume compatibility across a MINOR bump the way it could once `v1.0.0` ships.
- **MAJOR (once `v1` ships):** we avoid breaking changes to the runtime contract within `v1.x`. A breaking change ships as `v2.0.0` with migration notes in the CHANGELOG.
- **MINOR:** safe to redeploy without changing caller URLs, event filters, or env vars, unless you opt into a new capability.
- **PATCH:** drop-in image replacement; upgrade recommended for security fixes.
- **Migrations:** every release's migrations must be safe to run while the *previous* image is still serving (expand, then contract across two releases); a migration that is not is a MAJOR change.

## Supported releases

| Version | Status | Image tag | Notes |
|---------|--------|-----------|-------|
| *(none tagged)* | **Unreleased** | — | [CHANGELOG.md](CHANGELOG.md) lives entirely under `Unreleased`. `pyproject.toml`'s `version` is `0.1.0` — bump it in lockstep with the first Git tag; do not treat it as a published release. No live deployment yet, so no support window has started |
| `< v0.1.0` | — | — | No tagged Git releases |

**This service is not yet safe to release even once `make ci` is green.** The blockers, from `docs/implementation-plan.md`:

- No Worker Pool exists, so long-form generation (W-1, W-6, A-5) can be started but nothing executes it.
- Nothing has run against a real Temporal server, Model Gateway, Tender, IAM or S3; the contracts for those peers are assumptions.
- The Glue event encoder (LD-82) is not built, so published events are not framed for the schema registry.
- `/ready` does not yet reflect the outbox relay.

Resolve or consciously accept each before a tag is cut — not by working around them here.

This service has no formal support-window policy yet, since nothing is running in production against a tagged release. Once a `v1.0.0` ships to a real environment, this section will define how long a superseded major line receives security-only fixes (expect the platform convention the sibling services use: security fixes only, for a period the platform team sets).

## Consume a release

This service is **not** consumed via `pip install` — do not add it as a dependency of another service. It is consumed as a **container image** on the mesh (public `/api/v1/*`, internal `/api/v1/internal/*`, operator `/api/v1/operator/*`). Tender calls the generation and query routes, Ingestion calls the C-I routes, the Worker Pool reads and writes the shared `llm_generation` tables under its own role.

### Pull and verify the image

**Planned — there is no Dockerfile or image pipeline yet.** Once a tag exists:

```bash
docker pull ghcr.io/bcbp-solutions-fzc-llc/llm-retrieval-generation:v0.1.0
```

When the image jobs are added to `ci.yml` and a `release.yml` exists, sign keylessly with Sigstore/Cosign as `iam-org-membership` does, and verify before deploying (`@refs/heads/(main|master)` for `main` builds, `@refs/tags/` for tagged releases):

```bash
cosign verify \
  --certificate-identity-regexp "^https://github\.com/BCBP-SOLUTIONS-FZC-LLC/llm-retrieval-generation/.github/workflows/.*@refs/tags/.*$" \
  --certificate-oidc-issuer https://token.actions.githubusercontent.com \
  ghcr.io/bcbp-solutions-fzc-llc/llm-retrieval-generation@<digest>
```

### Image tag scheme

When the release workflow exists it should produce, per tag push (same scheme as the sibling services):

| Tag pattern | Produced for | Example (from `v1.2.3`) |
|-------------|--------------|--------------------------|
| `vMAJOR.MINOR.PATCH` | Every release | `v1.2.3` |
| `vMAJOR.MINOR` | Every release | `v1.2` |
| `vMAJOR` | Every release | `v1` |
| `latest` | Stable releases only (no pre-release suffix) | `latest` |

Separately, pushes to `main` should publish git-SHA / branch tags — pin production by a **release tag or digest**, not `latest` or an untagged `main` build.

| Pin style | Use when |
|-----------|----------|
| `vMAJOR.MINOR.PATCH` | Production; exact reproducibility (recommended — also enables digest verification) |
| `vMAJOR.MINOR` | Accept PATCH updates automatically |
| `vMAJOR` | Accept MINOR/PATCH updates automatically — not recommended before `v1.0.0` |
| `latest` / untagged `main` | Local/dev experiments only; never production |

### Run and migrate

There is no Helm chart yet. Deployment needs, in order:

1. **Migrations** from a one-shot job (not every replica), before the new image takes traffic: `python -m rag.infrastructure.persistence.migrate` with `CORPUS_MIGRATION_DATABASE_URL` / `GENERATION_MIGRATION_DATABASE_URL` set to a DDL-privileged role connecting **directly** to Postgres, never through PgBouncer (`make migrate` locally).
2. **The API**: `uvicorn --factory rag.presentation.main:create_app`, application DSNs through PgBouncer in transaction mode with `PG_BOUNCER_MODE=true` (startup refuses any other value).
3. **Sweeps** as CronJobs from the same image: `python -m rag.infrastructure.sweeps <name>|all` (exit 0 clean or skipped, 1 a tenant failed, 2 an alert, 64 usage).

The service reports `BUILD_VERSION` (`git describe --tags --always --dirty`, set by `make run`; an image build should set it from the tag).

## Maintainer release process

**Planned.** There is no `release.yml` yet: today `.github/workflows/ci.yml` runs `validate-test` and `validate-quality` on pushes to `main` and on pull requests, and nothing is built or published. The process below is the target; steps marked *(manual until automated)* are what you do today.

When cutting a tagged release:

1. **Merge** all changes for the release to `main`. Confirm the blockers in the Supported Releases section are resolved or consciously accepted.

2. **Update `CHANGELOG.md`:** move `Unreleased` entries into a new `## [X.Y.Z] - YYYY-MM-DD` section. `changelog-check.yml` already fails a PR that changes `src/`, `migrations/` or `schemas/` without touching the changelog; a release-time check that the section exists should be added with `release.yml`.

3. **Bump `pyproject.toml`'s `version`** to `X.Y.Z` and refresh the lockfile *(manual until automated)*:
   ```bash
   uv lock && make lock-check
   ```

4. **Regenerate the specs and run `make ci` locally** to confirm everything is green before tagging:
   ```bash
   make swag-check   # committed OpenAPI / AsyncAPI / event schemas match the code
   make ci           # fmt-check + lint + typecheck + test-ci (needs Docker)
   make test-e2e     # real app over a real RLS Postgres
   ```
   `validate-test.yml`'s coverage gate is **95%** (`.github/scripts/coverage-gate.sh`); measured coverage is about 97.8% for unit + integration.

5. **Create and push an annotated tag**:
   ```bash
   git tag -a v0.1.0 -m "v0.1.0"
   git push origin v0.1.0
   ```

6. **The release workflow** (to be added as `.github/workflows/release.yml`) should run, modeled on `iam-org-membership`'s:
   - `validate-test` / `validate-quality` — the same reusable gates `ci.yml` uses, re-run at the tagged commit.
   - `build` — verify the tag matches HEAD and the CHANGELOG entry exists.
   - `docker` — build and push the semver-tagged image, CVE-scan it (fail on CRITICAL/HIGH), generate an SBOM and provenance, sign with Cosign, verify the signature.
   - `publish` — create the GitHub Release with the CHANGELOG section as notes.

   Whether a live-cluster `deploy-gate` is mandatory (as in `iam-org-membership`) or skippable (as in `iam-realm-provisioner`) is a platform decision to make when this is written.

7. **Notify consumers** — Tender, Ingestion, IAM, the Worker Pool owners, the Model Gateway owners, and whoever owns the deployment — with upgrade notes if MINOR or MAJOR. Coordinate anything that touches a published event `type`, a route ID, a consumed event's expected shape, or a table the Worker Pool writes first — those are the highest-blast-radius parts of this contract.

### Pre-release tags (optional)

| Tag pattern | Meaning |
|-------------|---------|
| `v1.1.0-rc.1` | Release candidate; not for production unless approved |
| `v1.1.0-beta.1` | Early integration testing |

Both should match a `v[0-9]*.[0-9]*.[0-9]*-*` tag trigger and produce a signed, scanned image — but never a `latest` tag.

## Compatibility matrix

| retrieval-generation | Python (`pyproject.toml`) | Shared platform libraries (`uv.lock`) | PostgreSQL | `platform-schemagov` |
|---|---|---|---|---|
| Unreleased (`main`) | `>=3.12` (ruff and mypy target 3.12) | `platform-fastapicommon` 0.4.1, `pgcommon` 0.4.1, `eventcommon` 0.4.0 (constraints `>=0.4,<0.5`) | 18 (`uuidv7()`; tests run `postgres:18`) | `0.4` (`SCHEMA_GOV_IMAGE` in the Makefile) |

Other pinned tooling: `uv` 0.13.1 (Makefile and CI), `temporalio>=1.7`, `qdrant-client>=1.9`, `redis>=5.0`, `boto3>=1.40.61`. None of the platform libraries is re-exported — a consumer of this *service* never needs them as a dependency. Callers depend on the HTTP/event contract, not on any Python package here.

## Planned (not yet in place)

Anything in this document marked **Planned** is the intended design, kept here so the process is agreed before the machinery exists. Each row is the equivalent of something `iam-org-membership` already has. When an item lands, change its status to **Done**, remove the "Planned" wording where it appears above, and update this file in the same change.

| Item | What it will do | Where this document refers to it | Status |
|------|-----------------|----------------------------------|--------|
| `Dockerfile` | Build the service image. The `api/` and `docs/swagger/` files must ship alongside the package, because `/asyncapi` and `/swagger` read them from the repo root | Consume a release; Run and migrate | Planned |
| Image jobs in `ci.yml` (build, scan, smoke, push) | Publish `main` images with git-SHA / branch tags | Image tag scheme | Planned |
| Cosign keyless signing | Sign every pushed image; consumers verify before deploying | Pull and verify the image | Planned |
| `.github/workflows/release.yml` | Tag-triggered validate → build → docker (CVE scan, SBOM, SLSA provenance, sign) → publish | Maintainer release process | Planned |
| `verify-release-tag.sh` / `verify-changelog-entry.sh` | Fail a release if the tag is not at `HEAD` or `CHANGELOG.md` has no `[X.Y.Z]` section | Maintainer release process, step 2 | Planned (today only `changelog-check.yml` runs, on PRs) |
| Deploy gate | Live deploy, digest check, rollout status and an error-rate check with automatic rollback. Whether it is mandatory or skippable is a platform decision | Maintainer release process, step 6 | Planned, undecided |
| Helm chart (`deploy/helm/`) | Deploy the API, the migrate job and the sweep CronJobs; chart `version` / `appVersion` bumped in step with the tag | Run and migrate | Planned |
| `BUILD_VERSION` set from the tag at image build | Report the released version at runtime | Run and migrate | Planned (today set from `git describe` by `make run`) |
| Support-window policy | How long a superseded major line gets security-only fixes | Supported releases | Planned (after the first `v1.0.0` ships) |
| Readiness reflects the outbox relay | `/ready` fails when the relay is down | Supported releases | Planned (signal exists as `app.state.relay_ready`, route not wired) |
| Glue event encoder (LD-82) | Frame published events for the schema registry | Supported releases | Planned |
| Production gateway route configuration (`deploy/model-gateway/`, Kubernetes) and the quota, Bedrock and self-hosted-rerank experiments | The local standalone gateway and its contract tests exist; the Kubernetes route config, per-tenant quota, fail-closed behaviour, real Bedrock streaming and a self-hosted reranker behind the gateway do not | Gateway, Consume a release | Planned |
| Worker Pool | Execute long-form generation, outline, stage and coherence steps | Supported releases | Planned, separate service |

**Done today** (so not planned): the SemVer rules, `CHANGELOG.md`, `changelog-check.yml`, the 95% coverage gate, `uv.lock` with `make lock-check`, and the generated spec drift checks (`make swag-check`).

## Related files

| File | Purpose |
|------|---------|
| [CHANGELOG.md](./CHANGELOG.md) | User-facing history per version — currently entirely `Unreleased` |
| [docs/lld/llm-lld-retrieval-generation-service.md](./docs/lld/llm-lld-retrieval-generation-service.md) | The runtime contract in full — §5 endpoints, §12 events, §17 configuration, §24 error taxonomy |
| [docs/implementation-plan.md](./docs/implementation-plan.md) | Build status, traceability matrix, known gaps and assumed peer contracts |
| [docs/worker-pool-contract.md](./docs/worker-pool-contract.md) | What the Worker Pool must honour: run input, signals, tables it writes |
| [docs/swagger/swagger.json](./docs/swagger/swagger.json) / [api/asyncapi.yaml](./api/asyncapi.yaml) | Generated REST and event contracts (`make swag`) |
| [schemas/events/](./schemas/events/) | JSON Schemas of the published events |
| [.github/workflows/ci.yml](./.github/workflows/ci.yml) | Current validation pipeline (no image jobs yet) |
| [pyproject.toml](./pyproject.toml) / [uv.lock](./uv.lock) | Project version, dependency constraints and the lockfile — not a consumable package |
