# Runbook: Outbox Dead Letter

**Alert:** `RagOutboxDeadLetters` *(proposed; see [`observability/runbooks.md`](observability/runbooks.md#ragoutboxdeadletters-proposed-critical))*
**Severity:** Critical — domain events are being lost to a table nobody reads.

## Symptoms
- `platform_dlq_messages_total{operation="outbox_publish"}` rising (once metrics are on), or rows appearing in `outbox_dead_letters`
- Downstream services (Tender, Ingestion, Usage) not seeing `CollectionProvisioned`, `GenerationCompleted` and similar events

## Immediate actions
1. Check **both** databases — each has its own outbox and relay. On `llm_corpus` and on `llm_generation`:
   ```sql
   SELECT id, event_type, tenant_id, attempts, last_error, failed_at
   FROM outbox_dead_letters ORDER BY failed_at DESC LIMIT 50;
   ```
2. Read `last_error` and the payload of a failed row.
3. Check the topic ARNs (`GENERATION_TOPIC_ARN`, `INDEX_TOPIC_ARN`) and SNS reachability from the pod.
4. Check the event `type` is registered and routes to the intended topic (`RoutingPublisher`): an unregistered type is routed to `llm-generation-events` **with a warning**, so a wrong topic is recoverable but a dead letter is not alerted on.

## Root causes
- SNS topic unreachable, wrong ARN, or missing publish permission.
- A payload that no longer fits its contract (enqueue validation normally refuses this inside the state change's transaction, so a failure here is usually the publisher side).
- Network partition between the pod and the SNS endpoint (locally: floci not running).

## Resolution
- Wrong ARN or permissions: fix the configuration and redeploy.
- To retry dead-lettered rows use `eventcommon.outbox.dlq.reprocess_dead_letters(pool, limit)` (or `reprocess_dead_letters_with(pool, DLQFilter(...), limit)` for one event type) against the pool of the affected database — never hand-written SQL against the tables.
- Confirm afterwards: `SELECT count(*) FROM outbox_events WHERE published_at IS NULL;` falls to zero and `outbox_dead_letters` stops growing.

## Prevention
- Keep `EVENTS_ENABLED=true` wherever events matter; with it false nothing is relayed and nothing alerts.
- `/ready` already reflects the relays (not ready before the first successful poll, or once one stops polling); a dead letter is a publish failure and does not fail readiness.
