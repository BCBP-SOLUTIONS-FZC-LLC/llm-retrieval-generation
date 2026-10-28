# Request Flows, Concurrency & Erasure

Diagrams are in [`ARCHITECTURE.md`](../ARCHITECTURE.md). This file is the written invariant catalogue.

## 8.1 R-1 Search

1. `require_identity` builds a `RequestIdentity` from `x-*` headers (API-1).
2. Scope resolution: scope cache → on a miss `llm_corpus` (collections, grants, active generation) under RLS and, for restricted tenders, TAC-4 (100 ms, one retry). Empty scope → an empty answer; a failed read → `503 SCOPE_STORE_UNAVAILABLE` (CG-4).
3. Pipeline: pack → refine (150 ms) → plan → embed (300 ms, one retry, breaker) → dense search through `corpusscope` (mandatory compound filter, `is_current`) + lexical search → RRF → optional rerank (90 ms, no retry) → sufficiency verdict.
4. A retrieval set is recorded per attempt (a widened retry writes another). `206` where any stage degraded.

## 8.2 R-2 / R-3 Answer and draft (SSE)

1. `ShortFormEngine.start()`: admission (§5.6 order: tenant status, store probe, scope, tender gate, packs, VER-3, eligibility, budget), then one transaction for the idempotency lookup, shell, run, section and `GenerationStarted`. Retrieval; insufficient or refused → a `200` verdict frame with `refusal`, run closed `cancelled`.
2. A producer task (strong reference on the engine) drafts through the Gateway (12 s answers, 40 s drafts), feeds a queue the SSE body drains, verifies, writes S3, and runs the completion transaction. A client disconnect cannot stop it (GEN-2).
3. Frames: provisional `draft` frames, then exactly one `verdict` frame with the evidence tier, or one `error` frame. A blocking compliance scope suppresses the draft phase (LD-22).
4. Failure before the first byte is HTTP; after it, an `error` frame. The blocking compliance path waits `SSE_HEARTBEAT_SECONDS` for the producer so an early failure is still HTTP.

## 8.3 R-4 Extraction

Synchronous, never streamed. Gateway deadline 90 s (capped by the 120 s request timeout). `206` with every field a gap and a `refusal` on insufficient retrieval; unknown `schema_ref` is `422 PACK_INVALID`. `document_ids` post-filters the retrieved set.

## 8.4 W-1 Start a long-form run

1. `AdmitGeneration.prepare` (the shared checks) and `execute_async`: one transaction writing the shell, the run (with `workflow_id` and `run_input`) and `GenerationStarted`.
2. After the commit, the Temporal client starts `GenerationWorkflow` on the queue chosen by tier (`llm-generation`, `-enterprise`, `-strict`, WP-10).
3. Engine unset or unreachable → `503 WORKFLOW_ENGINE_UNAVAILABLE`; the run stays `pending`; a repeat of the same `Idempotency-Key` starts it. A replay answers `200` for a run in any non-failed state.

## 8.5 W-3 / W-4 / W-5 / W-7 Signals

1. Re-resolve scope (ASYNC-11). Empty → one transaction: run `failed/scope_revoked`, artefact `failed`, sections closed, audit entry, `GenerationFailed`; answer `404`.
2. Otherwise one guarded `UPDATE … WHERE status AND record_version`; the returned version is `progress_seq`. W-5 writes the run and the artefact (`cancelled`) in one transaction.
3. The signal is sent **after** the row commit (LD-88). A repeat of a signal whose row already reads the post-state re-raises the lost signal. A lost W-7 signal is unrecoverable from here (W-8 refuses a `generating` run).

## 8.6 A-5 / A-6 / A-7 Regeneration

A-5: document scope, `202`, resolved through W-2; carries `instruction` (≤ 2000 chars) in `run_input`; rejects `review_context` (400). A-6: section scope, streamed like R-3, never writes the artefact row (so two sections of one artefact regenerate at once; two of one section are refused while the first is `generating`). A-7: resolves a surfaced fact conflict once; flags every other section with a version; regenerates none. All answer `OPTIMISTIC_LOCK_CONFLICT` for a run in flight, a section being generated, or an artefact with no document version (ERR-1).

## 8.7 C-I1 and the collection lifecycle

C-I1 resolves **or creates** (`201`/`200`), emitting `CollectionProvisioned`; a tender's collection id is a UUIDv5 so concurrent first documents resolve on insert. `TenderRestricted` creates a restricted row or announces restriction and promotes by a second row with atomic cutover (COL-7, COL-8); `TenderPurged` moves to `purging` and never deletes the row (COL-13); only offboarding deletes. C-I4 activates a `verified` generation atomically; C-I6 bumps the purge epoch; C-I8 takes the promotion copy result.

## 8.8 Consumers

Each SQS message: decode (Glue on the two IAM queues) → tenant must be a UUID agreeing with the payload → contract check → bind tenant → one use case → ledger row. Poison (non-UUID tenant such as `system`, schema violation, forged event) → DLQ + ack; unknown type → ack + log; anything else raises and SQS redelivers. PE-1 (ledger row in the effect's transaction) holds for `llm_generation` effects; corpus-side consumers run an idempotent use case, then write the ledger row.

## 8.9 Sweeps

`python -m rag.infrastructure.sweeps <name>|all`: orphan-artefacts, stalled-generations, idempotency-keys, retrieval-sets, graph-checkpoints, outbox, processed-events (per-consumer retention), audit-chain (alert, exit 2), tenant-purge phase two, stalled-promotions. Each holds `pg_try_advisory_xact_lock` per instance and skips if it cannot. `DR_MODE` disables the four destructive-under-skew sweeps and tenant purge. Not built: zombie-run, orphan-S3-object, retention.

## 8.10 Concurrency

| Concern | Mechanism |
|---|---|
| Versioned writes | `WHERE id=$1 AND record_version=$2`; the trigger owns the column |
| Gate transitions | One guarded `UPDATE`; concurrent approvals produce one winner |
| First documents | UUIDv5 primary key + `ON CONFLICT DO NOTHING` + existence check (COL-9) |
| Audit chain | `audit_chain_append()` serialises per tenant; covered by a concurrency test |
| Fan-out | 5 per document, 10 per tenant, 100 global |
| Sweeps | Advisory xact locks (session locks do not survive PgBouncer transaction mode) |

## 8.11 Erasure (ART-9, PE-*)

Tender purge and tenant offboarding delete by prefix (`tenants/<tenant>/artefacts/<artefact>/…` in S3) and by RLS-scoped rows in the order ART-9 fixes; retrieval sets get purge markers so R-5 flags purged chunks while keeping excerpts. A purged tenant returns `410 COLLECTION_PURGING`. **Not yet reached:** Valkey keys and evaluation samples (§21.1).
