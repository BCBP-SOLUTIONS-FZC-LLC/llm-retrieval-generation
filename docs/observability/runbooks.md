# Runbooks: llm-retrieval-generation alerts

Runbooks for the alerts this service **should** carry. **None of them is deployed yet**: there are no alert rules, and metrics are off by default ([README](README.md)). Each entry says what to watch today (logs, database state, sweep exit codes) and which metric it will use once the `llm` domain is registered. Names marked *(proposed)* are not final.

Common starting points:

- Logs: structured JSON on stdout; follow `trace_id` and `request_id`. Content is never logged, so debug from ids in the database.
- Health: `GET /health`, `/live`, `/ready`. **`/ready` does not yet reflect the outbox relay** (`app.state.relay_ready` exists; the probe is not wired), so a stalled relay will not fail readiness.
- Database: connect **direct to Postgres** as the DDL role for diagnosis (`docker compose exec postgres psql -U rag_admin -d llm_generation` locally); the app role cannot see other tenants' rows by design.
- Sweeps: `python -m rag.infrastructure.sweeps <name>`; exit `0` clean or skipped, `1` a tenant failed, `2` an alert, `64` usage.

## Availability

### RagServiceDown *(proposed)*

**Meaning.** No replica is healthy.

**Triage.**
1. Pod status and recent restarts; check `/live`.
2. Startup refuses deliberately for bad configuration: `PG_BOUNCER_MODE` not exactly `true` (`ConfigError`), an unknown `APP_ENV`, a metrics domain outside the platform registry, an unreachable database at the initial pool ping (`pgcommon: initial ping failed`).
3. A migration was not applied before the new image: look for missing-relation errors from the first request.

**Mitigate.** Roll back the deploy or fix the named setting.

## Isolation

### RagCrossTenantAccess *(proposed, critical)*

See [`runbook-rls-cross-tenant.md`](../runbook-rls-cross-tenant.md).

### RagScopeStoreUnavailable *(proposed)*

Requests answering `503 SCOPE_STORE_UNAVAILABLE`. See [`runbook-scope-store-unavailable.md`](../runbook-scope-store-unavailable.md).

## Events

### RagOutboxDeadLetters *(proposed, critical)*

Domain events are being lost to dead letters. See [`runbook-outbox-dead-letter.md`](../runbook-outbox-dead-letter.md).

### RagOutboxBacklog *(proposed)*

**Meaning.** Unpublished rows are accumulating: the relay is down, SNS is unreachable, or `EVENTS_ENABLED` is false.

**Triage.**
1. `SELECT count(*), min(created_at) FROM outbox_events WHERE published_at IS NULL;` on **both** `llm_corpus` and `llm_generation` (each has its own outbox and relay).
2. Is `EVENTS_ENABLED=true` on the replicas? With it false nothing relays, silently.
3. `last_error` and `attempts` on the oldest rows; check `GENERATION_TOPIC_ARN` / `INDEX_TOPIC_ARN` and SNS reachability.
4. `/ready` will not tell you (known gap).

**Mitigate.** Fix the cause; the relay drains the backlog on its own. A row that exhausts `OUTBOX_MAX_ATTEMPTS` moves to `outbox_dead_letters`.

### RagConsumerDlq *(proposed)*

**Meaning.** Messages reached a queue's DLQ at the fourth receive.

**Triage.**
1. Which queue (`llm-document-rag-q`, `domainprofile-llm-invalidate-q`, `iam-rag-purge-q`, `iam-rag-membership-q`, `tender-rag-q`, `usage-rag-q`) and why: the consumer forwards **poison** (a non-UUID tenant such as `system`, a schema violation, a forged event) and acknowledges it; an unknown type is acknowledged and logged.
2. A handler that raises is redelivered, not dead-lettered by us; the queue's own redrive policy (five receives) is the backstop.
3. For a payload the producer changed, compare with `schemas/events/` and `api/asyncapi.yaml`.

**Mitigate.** Fix the producer or the handler; redrive from the DLQ once fixed.

## Data integrity

### RagAuditChainBreak *(proposed, critical)*

See [`runbook-audit-chain-break.md`](../runbook-audit-chain-break.md).

### RagStalledGenerations *(proposed)*

**Meaning.** Artefacts stuck in `generating` or `pending`.

**Triage.**
1. Run `python -m rag.infrastructure.sweeps stalled-generations` and `orphan-artefacts` and read the outcome (`affected`, `failed_tenants`).
2. The orphan sweep needs a workflow engine to restart runs; with `TEMPORAL_ADDRESS` unset it **skips** (never a false verdict).
3. A long-form run with no Worker Pool will sit `pending` — that is the known gap, not an incident.
4. A-5 runs on a `complete` artefact are never seen by the orphan sweep; only the client's repeat restarts a failed A-5 start.

### RagPurgeIncomplete *(proposed)*

**Meaning.** A tender purge or tenant offboarding did not finish.

**Triage.**
1. `python -m rag.infrastructure.sweeps tenant-purge` (phase two) and `stalled-promotions`.
2. `DR_MODE=true` disables the tenant purge and four destructive sweeps in the use case itself — confirm it is not set.
3. Known gap: tenant purge does not yet reach Valkey keys or evaluation samples (§21.1).

## Dependencies

### RagGatewayUnavailable *(proposed)*

**Meaning.** Model calls are failing.

**Behaviour.** Retrieval serves lexical-only with a degradation marker; generation answers `503`; a quota or residency refusal is surfaced as such and **never retried**. Token counting falls back to an over-estimate.

**Triage.** Gateway health and route configuration (this team owns the Gateway); `GATEWAY_BASE_URL`; the routes the client calls (`/v1/embeddings`, `/cohere/v2/rerank`, `/v1/chat/completions`, `/tokenize`, `/v1/models`) — all but `/v1/models` were confirmed against Agent Router v1.2.0 with `make test-gateway`, and the route configuration in use must still serve them (OQ-92/93).

### RagWorkflowEngineUnavailable *(proposed)*

**Meaning.** `503 WORKFLOW_ENGINE_UNAVAILABLE` on W-1, W-3..W-7, A-5.

**Triage.** `TEMPORAL_ADDRESS` set and reachable? Runs admitted before the outage stay `pending`; a repeat of the same `Idempotency-Key` (W-1/W-6) or `base_version_id` (A-5) starts them. A signal lost after a committed gate transition is **unrecoverable from here** for W-7 (W-8 refuses a `generating` run) — escalate to the Worker Pool owners.

## Performance

### RagStreamFirstFrameSlow *(proposed)*

`first_frame_latency_seconds` and `answer_first_token_seconds` rising. Check Gateway latency first (draft deadline 12 s for answers, 40 s for drafts), then retrieval stage durations (`retrieval_stage_duration_seconds` by `stage`), then PgBouncer saturation (`PG_MAX_CONNS` must equal the pool size).
