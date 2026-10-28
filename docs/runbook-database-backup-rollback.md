# Runbook: Database Backup, Migration Rollback and Restore

**Applies to:** the two PostgreSQL databases, `llm_corpus` and `llm_generation`.
**Not covered:** Qdrant, the lexical index and the document store (Ingestion's), Temporal's own database (the Worker Pool's), and S3 artefact content (versioning and replication are bucket settings the platform owns).

## What holds state

| Store | Contents | Loss means |
|---|---|---|
| `llm_corpus` | Collections, grants, index generations, retention policies, tenant lifecycle, its own outbox | Scope answers are wrong or empty: every request is refused or over-refused. Rebuilding is not possible from Ingestion alone (grants and lifecycle are this service's). |
| `llm_generation` | Artefacts and versions, sections, runs, retrieval record, verification, the audit chain, the inbox ledger, its own outbox | Customer-visible generation history and the tamper-evident audit record are gone. |

The two cannot be backed up as one consistent snapshot, and no foreign key crosses them, so a restore of one never needs the other to roll back.

## Backups

Backups are the platform's (RDS automated backups with point-in-time recovery, and a manual snapshot before every release). This service requires:

- **PITR enabled on both databases**, retention at least as long as the audit-chain verification window plus the sweep soak (`TENANT_PURGE_SOAK_DAYS`, default 7): 14 days minimum.
- **A manual snapshot of both databases immediately before a release that carries a migration**, named after the release tag. The migration Job is the point of no return for any non-additive change.
- **Restores are rehearsed** against a scratch instance at least once a quarter: restore, run `make migrate`-equivalent (`python -m rag.infrastructure.persistence.migrate`), then the audit-chain sweep (`python -m rag.infrastructure.sweeps audit-chain`), which must exit `0`.

## Rolling a release back

Migrations are per database, monotonic and each has a `.down.sql` (`migrations/README.md`). The rules that make a rollback safe:

1. **Additive first.** A release that adds a column or table is rolled back by redeploying the previous image: the old code ignores the new objects. Do **not** run the `.down.sql` in this case; leaving the objects is harmless.
2. **Destructive changes ship one release later**, after the release that stopped using the object. If a destructive migration already ran, the previous image may not start: restore from the pre-release snapshot instead of editing the schema by hand.
3. **The `.down.sql` files are for development and for the migration round-trip test**, not for production rollback. `tests/repository/test_migrations_roundtrip.py` proves they leave an empty database as it was; they do not preserve data.
4. **The library schemas** (`outbox_*`, `processed_events`, `inbox_migrations`) belong to `eventcommon` and are advanced by upgrading the library, never rolled back here.

Procedure for a bad release that has not migrated: `helm rollback <release>` (the migration Job is a pre-upgrade hook and did not run, or failed and stopped the release).
Procedure for a bad release that migrated additively: `helm rollback <release>`; leave the schema.
Procedure for a bad release that migrated destructively: stop traffic, restore both databases from the pre-release snapshots (point-in-time to just before the migration Job started), then `helm rollback`. Events published since the snapshot are not recalled: tell Tender and Ingestion consumers, whose handlers are idempotent on `event_id`.

## After any restore

1. Run the audit-chain sweep on `llm_generation`. A break after a restore means rows were lost between the chain head and the restore point: it **pages by design** and is never self-healed ([`runbook-audit-chain-break.md`](runbook-audit-chain-break.md)).
2. Check both outboxes: `SELECT count(*) FROM outbox_events WHERE published_at IS NULL;`. Rows from before the restore point will be relayed again; consumers dedupe on the inbox ledger.
3. A restored `processed_events` is older than the queue's state: messages already acted on may be redelivered and, if the ledger row is gone, applied twice. Effects must be idempotent (they are: PE-1), but compare the generation counts per tenant against the event stream for the restored window.
4. Evict Valkey caches (`profile`, `placement`, retrieval sets); a stale cache can serve scope or placement from the lost state.
5. Re-run `make rls-check` equivalents in staging before reopening: confirm `FORCE` RLS and the grants survived (`tests/repository/test_migrations_roundtrip.py` lists them).
