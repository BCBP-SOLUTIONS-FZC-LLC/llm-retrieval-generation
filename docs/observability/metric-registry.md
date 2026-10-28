# Metric inventory: llm-retrieval-generation

Every Tier-3 collector this service declares, by group. Names are listed without the `llm_retrieval_generation_` prefix. Written from `METRIC_NAMES` and `LABELS` in `src/rag/infrastructure/observability/` (the unit tests name-check them against the platform registry); **update this file in the same change as any collector**. All of it is dormant until the `llm` metrics domain is registered and `ENABLE_METRICS=true` ([README](README.md)).

The tables list each metric's own labels only; the platform adds `domain`, `service` and `environment`.

## Tier 1: platform-shared metrics (emitted by the platform libraries)

Not constructed here. `platform-fastapicommon` records `platform_http_*` and build info; `eventcommon` records `platform_messages_*`, `platform_outbox_*`, `platform_dlq_messages_total` and the inbox duplicate counter; `pgcommon` records `platform_db_*` per pool. See each library's own inventory.

## Tier 3: this service (`llm_retrieval_generation_*`)

### Retrieval (`observability/metrics.py`)

| Metric | Type | Labels | Meaning |
|---|---|---|---|
| `retrieval_stage_duration_seconds` | histogram | `stage` (`scope`, `pack`, `refine`, `plan`, `embed`, `search`, `fusion`, `rerank`, `assembly`, else `other`) | Wall time per retrieval stage |
| `degradation_marker_total` | counter | `stage`, `reason` | A RET-1 degradation marker returned on a response |
| `sufficiency_verdict_total` | counter | `verdict`, `section_type` | Sufficiency verdicts by pack-declared section type (else `other`) |
| `retrieval_verdict_total` | counter | `verdict` | The reduced retrieval verdict (`sufficient`, `insufficient`, `failed`) |
| `required_input_gap_total` | counter | `missing_role` | A required role missing from the resolved scope (§6.9) |
| `rerank_skipped_total` | counter | `reason` | Rerank skipped or failed |
| `scope_resolution_empty_total` | counter | — | A resolved scope with no collections — a spike is a misconfigured grant set or a probing caller |
| `candidate_depth_band_total` | counter | `profile_source`, `band` (`1-8`, `9-16`, `17-24`, `25-48`, `49+`) | Candidate depth per band (a counter, OBS-3) |
| `widened_retry_total` | counter | — | D-16's single widened retry |
| `refiner_latency_seconds` | histogram | `refiner` (`query`, `instruction`) | Refiner call latency; the query refiner's budget is 150 ms, the instruction refiner's 3 s |
| `retrieval_cache_hit_ratio` | gauge | — | Running hit ratio of the retrieval-set cache |

### Generation and verification (`observability/generation_metrics.py`)

| Metric | Type | Labels | Meaning |
|---|---|---|---|
| `claim_outcome_total` | counter | `outcome` (`supported`, `unsupported`, `miscited`, `gap`) | Per-claim verification outcome |
| `verification_tier_total` | counter | `tier_decided` | Ladder tier (0–3) that settled a claim |
| `groundedness_band_total` | counter | `band` | Groundedness score bands |
| `evidence_tier_total` | counter | `tier` (`NOT_COMPUTED`, `INSUFFICIENT`, `PARTIAL`, `SUPPORTED`) | Derived evidence tiers |
| `tier0_disagreement_band_total` | counter | `band` | Tier-0 self-consistency disagreement bands (a counter, OBS-3) |
| `failure_policy_total` | counter | `action` | Failure policy applied to a section |
| `completeness_item_missed_total` | counter | `section_type`, `item` | A mandatory completeness item missed |
| `gap_marker_total` | counter | — | Gap claims |
| `tokens_total` | counter | `task_class`, `model_profile`, `capability` | Tokens reported by the Gateway |
| `delivery_state_total` | counter | `delivery` (`delivered`, `disconnected`, `not_applicable`) | How a streamed answer ended |
| `idempotent_replay_total` | counter | — | An `Idempotency-Key` replay |
| `answer_first_token_seconds` | histogram | — | Time to the first draft frame |
| `first_frame_latency_seconds` | histogram | `suppressed` | Time to the first frame, whether or not the draft phase was suppressed |
| `answer_verified_seconds` | histogram | — | R-2: answer to verdict frame |
| `draft_verified_seconds` | histogram | — | R-3: draft to verdict frame |
| `extraction_duration_seconds` | histogram | — | R-4 duration |
| `verdict_frame_delivered_ratio` | gauge | — | Running share of verdict frames delivered |

### Workflows (`observability/workflow_metrics.py`)

| Metric | Type | Labels | Meaning |
|---|---|---|---|
| `workflow_start_total` | counter | `outcome` | Workflow start result; a `failed` start leaves a recoverable `pending` run |
| `workflow_signal_total` | counter | `signal`, `outcome` | Signal result; a failure after commit strands the run until repeated |
| `gate_action_total` | counter | `action` | An accepted gate or resume action |
| `scope_revoked_async_total` | counter | — | A run ended `failed/scope_revoked` by scope re-resolution |
| `regeneration_total` | counter | `scope`, `origin` | A regeneration admitted |
| `fact_conflict_resolution_total` | counter | `resolution` (`keep_established`, `update_fact`, `dismiss`) | A fact conflict resolved |

## Not built

- `refiner_edit_rate` (needs `user_action` capture, not built).
- Event-consumer and relay lag beyond what `eventcommon` emits.
- Names added beyond the LLD table (`workflow_*`, `gate_action_total`, `fact_conflict_resolution_total`, the generation additions) are **additions, not LLD names**; confirm them with the platform team when the domain is registered.
