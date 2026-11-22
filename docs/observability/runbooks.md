# Runbooks: llm-retrieval-generation alerts

Runbooks for the alerts this service **should** carry. **None of them is deployed yet**: there are no alert rules, and metrics are off by default ([README](README.md)). Each entry says what to watch today (logs, database state, sweep exit codes) and which metric it will use once the `llm` domain is registered. Names marked *(proposed)* are not final.

Common starting points:

- Logs: structured JSON on stdout; follow `trace_id` and `request_id`. Content is never logged, so debug from ids in the database.
- Health: `GET /health`, `/live`, `/ready`. `/ready` returns 503 when `EVENTS_ENABLED=true` and an outbox relay has not yet polled successfully or has not completed a store round trip within `3 x OUTBOX_POLL_INTERVAL + 30 s` (`Relays.ready`); `/ready` is also 503 whenever `llm_corpus` or `llm_generation` does not accept a write transaction on its primary (an outage, a hung database, or a read-only standby) within 0.6 s: look for the `not_ready` log (`failed` names the databases, `causes` the exception types) and `ready_again`; the answer is cached for 1 s. `/live` and `/health` are unaffected, so a stalled relay or an outage leaves rotation without a restart. A relay hung inside a publish is bounded by `OUTBOX_PUBLISH_TIMEOUT`, not detected by the probe.
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
4. `/ready` goes 503 once a relay stops polling (a database or loop failure); a growing backlog with `/ready` still 200 points at SNS or a bad ARN, not the relay loop.

**Mitigate.** Fix the cause; the relay drains the backlog on its own. A row that exhausts `OUTBOX_MAX_ATTEMPTS` moves to `outbox_dead_letters`.

### RagConsumerDlq *(proposed)*

**Meaning.** Messages reached a queue's DLQ at the fourth receive.

**Triage.**
1. Which queue (`llm-document-rag-q`, `domainprofile-llm-invalidate-q`, `membership-rag-q`, `tender-rag-q`, `usage-rag-q`) and why: the consumer forwards **poison** (a non-UUID tenant such as `system`, a schema violation, a forged event) and acknowledges it; an unknown type is acknowledged and logged.
2. A handler that raises is redelivered, not dead-lettered by us; the queue's own redrive policy (five receives) is the backstop.
3. For a payload the producer changed, compare with `schemas/events/` and `api/asyncapi.yaml`.

**Mitigate.** Fix the producer or the handler; redrive from the DLQ once fixed.

## Data integrity

### RagAuditChainBreak *(proposed, critical)*

See [`runbook-audit-chain-break.md`](../runbook-audit-chain-break.md).

### RagStalledGenerations *(proposed)*

**Meaning.** Artefacts stuck in `generating` or `pending`. The stalled sweep acts on **synchronous** runs only; an async run is never failed for being quiet (see `RagQuietAsyncRuns`).

**Triage.**
1. Run `python -m rag.infrastructure.sweeps stalled-generations` and `orphan-artefacts` and read the outcome (`affected`, `failed_tenants`).
2. The orphan sweep needs a workflow engine to restart runs; with `TEMPORAL_ADDRESS` unset it **skips** (never a false verdict).
3. A long-form run with no Worker Pool will sit `pending` — that is the known gap, not an incident.
4. A-5 runs on a `complete` artefact are never seen by the orphan sweep; only the client's repeat restarts a failed A-5 start.

### RagQuietAsyncRuns *(proposed)*

**Meaning.** The `quiet-async-runs` sweep (CronJob, every 15 minutes, exit code `2`) found a `generating` async run whose row and every section have had no write for `QUIET_ASYNC_RUN_SECONDS` (default 3,600). It reads the database only, so it cannot tell a lost workflow from a slow one. It alerts and changes nothing.

**Triage.**
1. The log line `ALERT quiet_async_run` names `tenant_id`, `run_id`, `artefact_id`, `stage` and `quiet_since`.
2. Is a long stage the cause? A stage 2 of many sections at five in parallel can run an hour; a tenant waiting for a slot, the facts step and the coherence pass also write no section. Raise `QUIET_ASYNC_RUN_SECONDS` if the alert is the pattern and not an incident.
3. Is the Worker Pool up (pods polling the run's queue, `llm-generation*` or `llm-extraction*`)? Temporal's UI shows the workflow under the run's `workflow_id` (`generation_runs.workflow_id`).
4. A workflow that is gone: restart it with W-8 (`POST /api/v1/operator/generations/{id}/rematerialise`, operator only, ticket required). It accepts a `generating` run; a live workflow answers `409`, so a healthy run is never started twice, and the new workflow continues from `current_stage` and skips every finished section.

### RagOutboxAgeWorkers *(proposed)*

**Meaning.** Events the Worker Pool wrote (`OutlineReady`, `SectionCompleted`, `GenerationCompleted`, ...) wait in `llm_generation.outbox_events`: the outbox relay runs only in the API pods (`rag_activity` may insert and nothing else), so worker events stop when the API pods are down.

**Triage.** As `RagOutboxBacklog`: `SELECT count(*), min(created_at) FROM outbox_events WHERE published_at IS NULL` on `llm_generation`; are the API pods running, is `EVENTS_ENABLED=true`. Alert on the age of the oldest unpublished row, not on a count.

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

**Triage.** `TEMPORAL_ADDRESS` set and reachable? Runs admitted before the outage stay `pending`; a repeat of the same `Idempotency-Key` (W-1/W-6) or `base_version_id` (A-5) starts them. A signal lost after a committed gate transition is recoverable by an operator through W-8 once the run is `generating` (alerted by `RagQuietAsyncRuns`); before this revision it was **unrecoverable from here** for W-7 (W-8 refuses a `generating` run) — escalate to the Worker Pool owners.

## Performance

### RagStreamFirstFrameSlow *(proposed)*

`first_frame_latency_seconds` and `answer_first_token_seconds` rising. Check Gateway latency first (draft deadline 12 s for answers, 40 s for drafts), then retrieval stage durations (`retrieval_stage_duration_seconds` by `stage`), then PgBouncer saturation (`PG_MAX_CONNS` must equal the pool size).
