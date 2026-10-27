# Worker Pool contract (what retrieval-generation sends and relies on)

Source: `docs/lld/llm-lld-retrieval-generation-service.md` §9 (execution model, §9.2 input, §9.3 WP-*, §9.4 gate),
§4.3.6 (run status table), §12.3 (events), §13.3 / §13.11 (flows), §14.3, §18.9. Scope cut S-1: the workflow and
activity **logic** is the Worker Pool's (`llm-workers`, its own deployable). This service holds a Temporal client for
two calls, `start_workflow` and `signal_workflow` (LD-88), and the tables the workflow reads and writes. It never
queries a workflow (ASYNC-5). Everything marked **(assumed)** is this service's choice, not in the LLD, and must be
confirmed by the Worker Pool owner.

## 1. Start

| Argument | Value |
|---|---|
| Namespace | `llm-generation` (`TEMPORAL_NAMESPACE`) |
| Workflow type **(assumed)** | `GenerationWorkflow` (W-1, A-5) or `ExtractionWorkflow` (W-6) |
| `workflow_id` | `generation_runs.workflow_id` (RUN-2): `{artefact_id}` for an original, `regen:{base_version_id}` for an A-5 regeneration. Unique per tenant (`uq_run_workflow`) |
| Task queue (WP-10) **(names assumed)** | `llm-generation`; `llm-generation-enterprise` for `plan = enterprise`; `llm-generation-strict` for any non-standard `residency_mode` (strict wins over plan) |
| Id reuse / conflict policy | W-1, W-6, A-5, orphan sweep: `REJECT_DUPLICATE` + `FAIL`; `WorkflowAlreadyStarted` is treated as success (ASYNC-2). W-8 only: `ALLOW_DUPLICATE` + `FAIL` (a closed id may be reused, a live one answers `409`) |
| Timeout / retry | 2 s per call, 2 retries with backoff (0.2 s, 0.5 s); failure is `503 WORKFLOW_ENGINE_UNAVAILABLE` and leaves the run `pending` (recoverable by the orphan sweep) |
| Input | `GenerationWorkflowInput`, below. The same JSON is stored in `generation_runs.run_input`, so a lost workflow restarts with it (RUN-8) |

`GenerationWorkflowInput` (JSON object; identifiers and a scope seed only, ASYNC-12 / GEN-11 / WP-9):

```
tenant_id, tender_id, principal_id, trace_id, correlation_id
artefact_id, generation_run_id, capability, target_ref, requested_mode
plan, residency_mode, domain_profile_version, language_pack_version   // pinned for the run (ASYNC-3)
scope_seed { tender_id, collection_ids[], collection_types[] }        // a seed, never an authority (ASYNC-6)
kind            "document" | "extraction"
outline_gate    bool (a pack may force true)
origin          "original" | "regeneration"
base_version_id (regeneration) , instruction (A-5: the reviewer's own words, <= 2000 chars)
schema_ref, trigger_document_version_id   (W-6 only)
recipients[ {kind, id} ]                  // echoed on every event of the run (LD-83, provisional)
```

There are **no departments and no roles** in it (ASYNC-11). The workflow and its activities must re-read both from IAM
per section and re-resolve scope (WP-4, SCOPE-3). A-6 (section regeneration) is synchronous and never starts a workflow.
`review_context` (REGEN-3) is never in any run input.

## 2. Signals (all raised **after** the row transaction commits, LD-88)

Addressed to the run's `workflow_id`. A signal to a workflow that no longer exists is reported to the caller as `503`
(the run is a zombie; W-8 restarts it).

| Signal **(names assumed)** | Payload | Raised by | What the workflow does |
|---|---|---|---|
| `approve_outline` | `{outline_version}` | W-3 | Leaves the gate wait, runs stage 1. The row is already `generating`, `outline_state = approved`, `current_stage = stage1` |
| `revise_outline` | `{outline_version (already incremented), sections: [{ref, type}]}` | W-4 | Loops to the outline activity with the edited outline (references and types only, never content). The row is already `outline_building`, `outline_state = revising`. The outline activity moves `revising -> building -> ready` and rewrites the section rows |
| `cancel` | `{reason}` | W-5, and best effort after a `scope_revoked` end | Stops. The row is already `cancelled` (or `failed/scope_revoked`) and in-flight sections are already closed (AS-8). **A signal that finds the run terminal in the database must end the workflow without writing the run again** |
| `resume` | none | W-7, quota-restored consumer (ASYNC-8) | Leaves the suspended wait and continues; each section activity is idempotent (ASYNC-14). The row is already `generating` with `failure_reason` cleared |

A repeated W-3 or W-5 whose first signal was lost re-raises the signal: the workflow must treat a duplicate `approve_outline`
(same `outline_version`) and a duplicate `cancel` as no-ops.

## 3. Who writes which row (RUN-7: one writer per edge)

This service writes (always in one transaction with its event and audit entry):

| Edge | Route |
|---|---|
| `-> pending` (+ shell, `workflow_id`, `run_input`, `GenerationStarted`) | W-1, W-6, A-5 |
| `awaiting_outline_approval -> generating` (`outline_state = approved`, `current_stage = stage1`) | W-3 |
| `awaiting_outline_approval -> outline_building` (`outline_state = revising`, **`outline_version` + 1, only here**, `current_stage = outline`) | W-4 |
| `suspended -> generating` (`failure_reason` cleared) | W-7, quota consumer |
| any non-terminal `-> cancelled` (artefact `cancelled` where `pending/generating`, in-flight sections closed) | W-5 |
| any non-terminal `-> failed/scope_revoked` (artefact `failed` where live, sections closed, `GenerationFailed`) | W-3, W-4, W-5, W-7 on an empty re-resolved scope (ASYNC-15) |

The Worker Pool writes (as `rag_activity`): `pending -> outline_building`, `outline_building -> awaiting_outline_approval`
(sets `outline_state = ready`, `outline_version`, `outline_ready_at`, `gate_expires_at`), `pending -> generating` (no gate),
`generating -> suspended` (**with `failure_reason = quota_exhausted`**: the quota consumer selects on it),
`generating -> complete | failed` (a failed run always carries `failure_reason`, ck_run_failure),
`awaiting_outline_approval -> failed/gate_expired` (WP-15), `current_stage` at each phase, `coherence` / `coherence_computed_at`
(WP-16), the section rows and versions, `document_facts`, `fact_conflicts` (detection), `graph_checkpoints`, the
verification rows, and the outbox rows for the events below.

`progress_seq` of `GenerationProgressed` is the run's `record_version` as returned by the very `UPDATE` that changed the state
(the table trigger bumps it on every write); this service uses it that way, and activities must too (EVT-13).

## 4. What the workflow is expected to do (WP-*, ASYNC-*)

| # | Expectation |
|---|---|
| ASYNC-9 | Workflow code does no I/O; every read and write is an activity |
| ASYNC-10 / WP-2 | One section's graph runs inside one activity and never spans two; checkpoints are an optimisation inside it |
| ASYNC-12 / WP-9 | No generated prose, chunk text, prompt text or document content in a workflow input, signal, activity result or history. The signals above carry identifiers, versions, and (W-4) section references and types |
| ASYNC-14 | A section activity writes a version only where the section is not already `complete`; the phase activities skip every `complete` section. A restarted workflow (W-8, orphan sweep) repeats only the interrupted section's token spend |
| WP-1 | Each section is its own child workflow with one activity (independently retryable and regenerable) |
| WP-3 | Activities bind the three GUCs (`app.tenant_id` and the identity pair) from the run input before any query |
| WP-4 | Activities re-resolve scope per section; an empty scope ends the run `failed/scope_revoked` and publishes `GenerationFailed` (completed sections stay durable); a narrowed scope proceeds with `scope_narrowed` on the section version |
| WP-5 / WP-15 | The gate is the `approve_outline` / `revise_outline` / `cancel` signals plus a timer at `gate_expires_at` (`REVIEW_GATE_DAYS`); on expiry an activity fails the run `gate_expired` and publishes `GenerationFailed` |
| WP-6 / WP-7 | Bounded parent history; stage-2 fan-out bounded at `FANOUT_MAX_PER_DOCUMENT` (5) per document and 10 per tenant by a semaphore in the workflow |
| WP-8 | Schema validation failures, quota denials and residency denials are non-retryable. A quota refusal suspends the run instead of ending it (LD-70) |
| WP-10 | Honour the three task queues above; a strict-residency tenant never runs on the shared queue |
| WP-12 | Temporal availability only affects W-1, W-6, A-5 and the signal routes (`503`); R-1 to R-4, A-6 and every read are unaffected |
| WP-13 | Publish the activity timeout, heartbeat and `maximum_attempts` (`ACTIVITY_START_TO_CLOSE_SECONDS`, `ACTIVITY_HEARTBEAT_SECONDS`, `ACTIVITY_MAX_ATTEMPTS`) |
| WP-14 | A global concurrency bound across tenants (`FANOUT_MAX_GLOBAL`) is still nobody's (§20 OQ-48) |
| WP-16 | Stage 1 populates `document_facts`; the coherence pass writes `generation_runs.coherence` and surfaces (never retries) a second conflict on a key |

Events the Worker Pool publishes (via the outbox, in the transaction of the state change): `OutlineReady`,
`GenerationProgressed` (every stage transition), `SectionCompleted`, `GenerationGapReported`, `GenerationCompleted`
(and `ArtefactRegenerated` where an A-5 run mints a document version, `minted_reason` per §4.3.2), `GenerationFailed`,
`ExtractionCompleted`. Events published by **this** service for these flows: `GenerationStarted` (every admission),
`GenerationProgressed` (W-3, W-4, W-7), `GenerationCancelled` (W-5), `GenerationFailed` (`scope_revoked` on a signal route),
`SectionCompleted` with `origin = regeneration` and `superseded_version_ids` (A-6). Every event carries the originating
principal and the `recipients` from the run input.

## 5. Recovery paths that rely on this contract

* **Failed start (ASYNC-2).** The shell, run and `GenerationStarted` are committed; the run stays `pending`. A client retry with
  the same `Idempotency-Key` (W-1/W-6) or the same `base_version_id` (A-5) starts it again; the orphan sweep starts a `pending`
  *artefact* again under the same `workflow_id` from `run_input` once (`ORPHAN_PENDING_SECONDS`). **Gap:** an A-5 run is `pending`
  on an artefact that is `complete`, which the sweep does not select; only the client's repeat recovers it.
* **Lost workflow (W-8, §18.9).** `POST /api/v1/operator/generations/{id}/rematerialise` (operator only, ticket required) starts the
  workflow again under the same id from `run_input` for a run `suspended` or `awaiting_outline_approval`; the new workflow
  continues from `current_stage`, re-arms the gate timer from `gate_expires_at`, and skips every `complete` section.
* **Quota restored (LD-70).** The `TenantQuotaRestored` consumer signals `resume` only for runs `suspended` with
  `failure_reason = quota_exhausted`, after re-reading the principal's memberships from IAM and re-resolving scope.
* **Signal lost after commit.** A repeat of W-3, W-4 (same `outline_version` and outline) or W-5 re-raises the signal while the
  row still reads the post-state. W-7 does not: a lost W-7 signal leaves the run `generating`, indistinguishable from a normal one,
  and W-8 refuses it (it restarts only `suspended` / `awaiting_outline_approval` runs, §5.7.14). It is not recoverable from this
  service today; the zombie-run sweep (§21.2) that would alert on it is not built. Recorded as a gap in the implementation plan.
