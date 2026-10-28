# Database Schema

Two PostgreSQL 18 databases, two migration ledgers (`pgcommon_migrations`), **no foreign key across them** (LLD §4.11). The migrations in `migrations/` are the source of truth; this file is the map. Numbering, conventions and the per-file table are in [`migrations/README.md`](../migrations/README.md).

## `llm_corpus` (5 tables)

| Table | Purpose |
|---|---|
| `collections` | One row per physical placement; `id` is UUIDv5 over exactly `uq_coll_tender`'s key; `status` ∈ `provisioning, active, frozen, purging, migration_failed, superseded`; `active_index_generation`, `purge_epoch`, `restricted`, `migration_id`, `record_version` |
| `collection_grants` | The last recorded overlay answer per user (`grant_level`: `search`, `answer`, `generate`) — diagnostic, never authoritative (CG-2) |
| `index_generations` | Per-collection generations; status `registered → building → verified → active → retired/failed`; activation only from `verified`, enforced by trigger |
| `retention_policies` | One per collection; `retain_days`, `legal_hold`, optimistic lock |
| `tenant_lifecycle` | One row per frozen or purged tenant; consulted by C-I1 when no collection row exists (`410 COLLECTION_PURGING`, `409 COLLECTION_FROZEN`) |

Plus the library's `outbox_events` / `outbox_dead_letters` / `outbox_migrations` (corpus events commit in this database's transactions, EVT-8).

## `llm_generation` (18 tables plus the library's outbox and inbox)

| Group | Tables |
|---|---|
| Artefacts | `artefacts` (kind, status, archive state), `artefact_versions`, `artefact_version_sections`, `artefact_sections`, `artefact_section_versions` |
| Runs | `generation_runs` (`workflow_id` unique, `run_input` with no authority or content, `record_version` doubles as `progress_seq`), `graph_checkpoints` (activity-written, pruned by a sweep) |
| Retrieval record | `retrieval_sets`, `retrieval_set_chunks` (240-character excerpts, purged ones flagged), `retrieval_set_purges`, `query_refinements`, `instruction_refinements` |
| Verification | `groundedness_reports`, `claim_verifications`, `completeness_checks` |
| Facts | `document_facts`, `fact_conflicts` |
| Audit and events | `artefact_audit_chain` (per tenant, `(tenant_id, seq)`, `prev_hash`/`entry_hash`), `processed_events` (the consumers' inbox ledger) |

Enums (`0001_types_and_functions`): `artefact_status` (`pending, generating, complete, cancelled, failed, superseded`), `run_status` (`pending, outline_building, awaiting_outline_approval, generating, suspended, complete, failed, cancelled`), `section_state` (`pending, generating, complete, gap, failed`), `evidence_tier` (`NOT_COMPUTED, INSUFFICIENT, PARTIAL, SUPPORTED`), `claim_outcome` (`supported, unsupported, miscited, gap`), `run_failure_reason`, `delivery_state`, `eligibility_state`, and others.

## Roles

| Role | Used by | Notes |
|---|---|---|
| `rag_owner` | Owns every object (NOLOGIN) | Migrations `SET ROLE rag_owner`; `FORCE` RLS binds it |
| `rag_app` | Request handlers and consumers (NOLOGIN group; login `rag_app_login` locally) | Non-superuser, no `BYPASSRLS` |
| `rag_activity` | Worker Pool activities | `INSERT` + sequence on the outbox; per-table grants on the activity tables |
| `rag_sweeper` | Sweeps' enumeration pools | Column-limited `SELECT` and a `FOR SELECT … USING (true)` policy per table; no writes |
| migration/DDL role | `migrate`, direct to Postgres | Never through PgBouncer (session-scoped advisory lock) |

## RLS

Every table except `processed_events` and the outbox has `ENABLE` **and** `FORCE ROW LEVEL SECURITY` and a uniform `tenant_isolation` policy on `tenant_id = current_setting('app.tenant_id')` (`collections` and `collection_grants` use a read/write pair). The GUC is set transaction-locally by the pool provider (RLS-6). **Superusers bypass RLS even with `FORCE`** — tests and the app use a non-superuser login. `0003` adds the `rag_sweeper` policies.

## Triggers and functions (§4.8.2)

- `bump_record_version()` and `touch_updated_at()`: `record_version` and `updated_at` maintained by trigger (LD-103); an `UPDATE` that changes nothing is skipped. Application code never sets `record_version`. `reject_column_change()` guards immutable columns.
- `reject_always` on append-only tables; `reject_update_except()` is kept for `artefact_versions` / `document_facts` (called with no arguments it is a no-op: empty `TG_ARGV` is `NULL`).
- `reject_generation_transition`: activation only from `verified`, using `pg_has_role` membership (deviation from the literal LLD check, which would reject every write).
- `reject_purged_change`: purged rows are immutable.
- Section and run transition tables enforced by trigger; `audit_chain_append()` builds the gapless hash chain with the built-in `sha256()` (no `pgcrypto` `digest()`).

## Migration conventions

- `NNNN_name.up.sql` + `NNNN_name.down.sql`, per instance, monotonic, never renumbered; `0000` roles, `0001` types and functions, `0002` tables, `0003` events and sweeps, `0004` (generation) workflows.
- The outbox schema is applied before the numbered sets (`migrate_all`).
- Primary keys default to `uuidv7()` (PostgreSQL 18).
- Additive first, destructive in a later release; every new tenant table gets the uniform policy, `FORCE`, and per-role grants.
