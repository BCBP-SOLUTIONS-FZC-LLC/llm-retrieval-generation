# Runbook: Audit Chain Break

**Alert:** `RagAuditChainBreak` *(proposed; see [`observability/runbooks.md`](observability/runbooks.md#ragauditchainbreak-proposed-critical))*
**Severity:** Critical — a break is a tamper signal. It is **never self-healed**.

## Symptoms
- `python -m rag.infrastructure.sweeps audit-chain` exits **2** (an alert) and logs `ALERT audit chain of tenant <id> breaks at seq <n>`.
- A CronJob for the `audit-chain` sweep failing with exit code 2.

## What the chain is
`artefact_audit_chain` is append-only and gapless **per tenant** (primary key `(tenant_id, seq)`): each row carries `payload_hash`, `prev_hash` and `entry_hash`, linking it to the previous row of the same tenant (`audit_chain_append()` computes them with the built-in `sha256()`, and the payload hash covers `seq` too). Updates and deletes are rejected by trigger (`reject_always`) — the roles hold `UPDATE` grants, so it is the trigger, not the grant, that protects the chain. The sweep reads each tenant's chain and reports the first break (`first_break` in the domain).

## Immediate actions
1. Read the sweep log line: the tenant and the sequence number of the first break.
2. **Do not repair or delete rows.** Preserve the table, the logs and the database's WAL/backups for the window around the break.
3. Inspect the rows around the break as the DDL role:
   ```sql
   SELECT seq, occurred_at, entry_type, action, artefact_id, prev_hash, entry_hash
   FROM artefact_audit_chain
   WHERE tenant_id = '<tenant id>' AND seq BETWEEN <n-5> AND <n+5> ORDER BY seq;
   ```
   A **gap** (missing `seq`) points at a delete or a rolled-back concurrent append; a **`prev_hash` that differs from the previous row's `entry_hash`**, or a recomputed hash that differs from `entry_hash`, points at an in-place change. Superusers bypass RLS, so run this as the DDL role only for diagnosis.

## Root causes to rule out
- A trigger disabled or dropped (`session_replication_role = replica`, a migration that recreated the table without the trigger). `rag_app` and `rag_activity` hold `UPDATE` grants on the table, so the trigger is the only thing standing between a bug or a role and an in-place edit — check `pg_trigger` for the immutability triggers.
- A concurrency defect in `audit_chain_append()` — the chain is covered by a concurrency test in `tests/repository`; a failure there is a regression, not tampering.
- A restore or logical copy that reordered rows.

## Resolution
- A confirmed tamper or unexplained gap goes to the security owners ([`.github/SECURITY.md`](../.github/SECURITY.md)) with the preserved evidence.
- A confirmed defect: fix it and add a regression test; the broken chain stays as evidence — a corrected chain is appended after it, never rewritten.
