# Runbook: Scope Store Unavailable

**Alert:** `RagScopeStoreUnavailable` *(proposed; see [`observability/runbooks.md`](observability/runbooks.md#ragscopestoreunavailable-proposed))*
**Severity:** High — retrieval, generation and C-I1 are refused rather than answered with a default (CG-4).

## Symptoms
- Responses `503 SCOPE_STORE_UNAVAILABLE` on search, answers, drafts, extractions, collection and artefact routes, and C-I1.
- Logs showing connection errors from the `llm_corpus` pool, Qdrant, or a TAC-4 refusal (`tac4_refused`).

## Why it refuses
Scope resolution depends on the collection tables (`llm_corpus`), the vector store, and — for restricted tenders — IAM's Tender ACL overlay (TAC-4). Where any of them cannot be read the request is refused: an empty list would look like "no access" and a default would leak. `CorpusTransactionRunner` maps connection-class failures (connection refused or lost, pool closed, acquire timeout) to this code; any other exception stays a `500` because a bug is not an outage.

## Triage
1. Which dependency? Pool errors point at `llm_corpus` (or PgBouncer); `tac4_refused` at IAM; Qdrant errors at the vector store (`QDRANT_URL`).
2. **PgBouncer:** `PG_MAX_CONNS` is per pod, while PgBouncer's `pool_size` is per db/user pair and shared by every pod; a pool smaller than the concurrent load times out, a larger one queues behind PgBouncer. Sizing across replicas: [Connection budget](../ARCHITECTURE.md#connection-budget).
3. **Valkey** is *not* the cause: a cache outage is a miss and the source is read.
4. **TAC-4:** `IAM_BASE_URL` set? A non-200 or non-string level refuses the request by design; `{"level": null}` is a successful no-access answer. The client budget is 100 ms with one retry, so a slow IAM looks like an outage.
5. Is the replica starting at all? An unreachable database at the initial ping stops startup (`pgcommon: initial ping failed`).

## Mitigate
- Restore the dependency; nothing needs replaying, requests succeed once scope resolves.
- Do **not** add a fallback scope or widen the cache TTL to paper over an outage: a cached scope can already make a purge visible up to 60 s late.
- Callers should retry with backoff; the response carries the standard problem body with `traceId`.
