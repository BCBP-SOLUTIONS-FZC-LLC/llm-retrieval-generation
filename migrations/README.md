# Migrations

Two instances, two directories, two ledgers (LLD §4.11). No foreign key crosses them.

```
migrations/corpus/       llm_corpus      (collections, grants, index_generations, retention, tenant_lifecycle)
migrations/generation/   llm_generation  (artefacts, runs, retrieval record, verification, audit chain, ...)
```

Runner: `pgcommon.migrate.Runner`, files named `NNNN_name.up.sql` + `NNNN_name.down.sql`, ledger
`pgcommon_migrations` in each database. Every `.up.sql` has a `.down.sql` that reverses it. Run from the
init container / pre-deploy job, never from a replica's boot path:

    python -m rag.infrastructure.persistence.migrate     # eventcommon outbox (+ inbox on llm_generation), then both sets

DDL role: `CORPUS_MIGRATION_DATABASE_URL` / `GENERATION_MIGRATION_DATABASE_URL` (direct to Postgres,
not PgBouncer; the runner's advisory lock is session-scoped).

## Numbering

`0000` and `0001` are reserved to the foundation. Every later slice takes the next free number **per
instance** (`0002`, `0003`, ... in merge order, no gaps, never renumbered). Names carry the instance, e.g.
`0002_corpus_tables`, `0002_generation_artefact_tables`.

| No. | corpus | generation |
|---|---|---|
| `0000_roles` | roles, schema grants, default privileges | same |
| `0001_types_and_functions` | enums (§4.7 + `grant_level`), §4.8.2 functions | enums (§4.2 + `evidence_tier`, `grant_level`), `pgcrypto`, §4.8.2 functions |
| `0002_corpus_tables` | five tables, indexes, `reject_generation_transition`, `reject_purged_change`, triggers, RLS + FORCE (uniform + read/write pair), per-table grants | |
| `0002_generation_tables` | | 18 tables, cross-table FKs (ART-2/AS-3/ART-9 cycles deferrable), indexes, §4.8.2 triggers + audit-chain functions, RLS + FORCE, §4.1 grants. No outbox and no `processed_events`: both are eventcommon's (applied before the numbered sets) |
| `0003_corpus_events_sweeps` | outbox grants for `rag_app`; role `rag_sweeper` + column grants and `FOR SELECT` policies on `tenant_lifecycle`, `collections` | |
| `0003_generation_events_sweeps` | | outbox grants (`rag_app`: S/I/U/D, `rag_activity`: INSERT + sequence); `rag_sweeper` on `artefacts`, `generation_runs`, `retrieval_sets`, `graph_checkpoints`, `artefact_audit_chain` |
| `0004_generation_workflows` | | `uq_run_workflow` (RUN-2: one run row per `workflow_id`), `idx_fc_section` |
| `0005_generation_*` ... | | later generation slices |

The LLD's phase split (§4.11: tables, then FKs, then triggers/RLS, then seed) is applied **per file**: a slice's
file creates its tables, then the foreign keys to tables that already exist, then triggers, indexes, RLS
(`ENABLE` + `FORCE` + policy) and grants. A foreign key to a table a later slice creates (the `artefacts` <->
`artefact_versions` <-> `generation_runs` cycle, `ART-2`, `AS-3`) is added by the later file with `ALTER TABLE`.
The corpus seed (reference collection + generation 1, `IG-3`, `COL-3`) is the corpus slice's last file.

## Conventions for DDL migrations

* Open with `SET ROLE rag_owner;` and finish with `RESET ROLE;` so every object is owned by `rag_owner`
  (nothing logs in as it; `FORCE ROW LEVEL SECURITY` then binds the owner, §4.1, §4.8.1). `0000` grants
  `rag_owner` to the migration role.
* Primary keys default to `uuidv7()` (PostgreSQL 18, LD-54).
* Every table except the library's own (`processed_events`, the `outbox_*` tables) gets the uniform
  `tenant_isolation` policy (§4.8.1, RLS-1); `collections` / `collection_grants` the read/write pair.

## Deviations from the LLD text (and why)

* §4.1 puts the per-table `GRANT`s in `0000`. A `GRANT ... ON <table>` fails before the table exists, so
  `0000` carries roles, schema usage and `ALTER DEFAULT PRIVILEGES FOR ROLE rag_owner` (gives `rag_app`
  `SELECT, INSERT, UPDATE` on every future table). The remaining per-table grants (`rag_app` `DELETE` lists,
  `REVOKE ... ON graph_checkpoints`, all `rag_activity` grants, corpus `tenant_lifecycle`) are issued in the file that creates the table.
* Roles are cluster-global: `0000` creates them idempotently and its `.down.sql` revokes this database's
  grants but keeps the roles and the migration role's `rag_owner` membership (the other instance may share the cluster).
* The `outbox_events` / `outbox_dead_letters` grants for `rag_app` / `rag_activity` are in `0003`, which
  therefore needs `eventcommon.outbox.apply_schema` to have run first: `migrate_all` applies the outbox
  **before** the numbered sets. The library's tables belong to the migration role, not `rag_owner`, so those
  grants are issued outside `SET ROLE rag_owner` (under it they would only warn and grant nothing).
* `0003` adds the NOLOGIN role `rag_sweeper`, the only way to ask "which tenants have work" (no role is
  `BYPASSRLS`): `SELECT` on filter columns only, one `FOR SELECT TO rag_sweeper USING (true)` policy per
  table, no write. The platform creates the sweep enumeration logins as members of it.
* `0002_corpus_tables` adds `collections.migration_id uuid` (nullable, set on a promotion target). The LLD
  (§5.7.16) defers it to Phase 2, but C-I8's `409 MIGRATION_MISMATCH` has nothing to compare against without it.
* The corpus seed (reference collection + generation 1, §4.11 `0005`) is not written yet: `reject_generation_transition`
  admits an INSERT only from `rag_app` / `rag_activity`, and the migration role is neither, so it needs a decision
  on how a migration may write as `rag_app` (or a platform-run provisioning job).
* The outbox schema (`eventcommon.apply_schema`) is applied to `llm_corpus` as well as `llm_generation`: corpus
  events (`IndexGenerationActivated`, `Collection*`) are enqueued on the connection of the `llm_corpus` transaction
  that changes the state (EVT-8, OBX-1), and one transaction cannot span both databases. One relay per table
  publishes through one routing publisher (`infrastructure/events/relay.py`); the grants are in `0003`.
* `0002_generation_tables`: `audit_chain_append()` hashes with built-in `sha256()` (no pgcrypto call), the
  append-only tables (`retrieval_sets`, `retrieval_set_chunks`, `retrieval_set_purges`,
  `artefact_section_versions`, `artefact_version_sections`) use `reject_always` on UPDATE because
  `reject_update_except()` called with no arguments never raises (empty `TG_ARGV` is NULL in plpgsql), and
  `idx_rs_tender` is added for the tender-purge delete by `tender_id`.
