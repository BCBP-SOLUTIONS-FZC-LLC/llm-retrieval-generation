# Runbook: Suspected Cross-Tenant Access

**Alert:** `RagCrossTenantAccess` *(proposed; see [`observability/runbooks.md`](observability/runbooks.md#ragcrosstenantaccess-proposed-critical))*
**Severity:** Critical — tenant isolation is the service's core guarantee.

## Symptoms
- A customer reports another tenant's collection, artefact, retrieval set or generated text.
- An `llm.cross_tenant_access` audit entry (W-8 rematerialise) that has no matching ticket.
- RLS isolation tests (`tests/repository`, `tests/e2e/test_security.py`) failing in CI.

## First: stop the bleeding
1. Identify the route and the caller from `request_id` / `trace_id`.
2. If a session-scoped tenant GUC is suspected, **restart the replicas** — pooled PgBouncer backends keep a session-scoped `app.tenant_id` until they are recycled.

## Where isolation can fail
1. **Session-scoped GUC (RLS-6).** `SET app.tenant_id` without `LOCAL`, or `set_config(..., false)`, survives into the next PgBouncer client. CI greps for it; check recent diffs and run the grep by hand:
   ```bash
   grep -REn '\bSET[[:space:]]+("?app\.tenant_id"?)[[:space:]]*=' --include='*.py' --include='*.sql' src/ migrations/ | grep -v 'SET LOCAL' | grep -v set_config
   ```
2. **`PG_BOUNCER_MODE`** not exactly `true`. The app refuses to start otherwise (`ConfigError`); confirm the running value.
3. **A superuser or `BYPASSRLS` login.** Superusers bypass RLS even with `FORCE`. The application DSN must be a non-superuser in `rag_app` (`rag_app_login` locally). Check:
   ```sql
   SELECT rolname, rolsuper, rolbypassrls FROM pg_roles WHERE rolname LIKE 'rag%';
   ```
   Only the migration role may be privileged; `rag_app`, `rag_activity`, `rag_sweeper` must be neither.
4. **A table without `FORCE`.** Every tenant table must have `relrowsecurity` and `relforcerowsecurity`:
   ```sql
   SELECT relname, relrowsecurity, relforcerowsecurity FROM pg_class
   WHERE relkind = 'r' AND relnamespace = 'public'::regnamespace
     AND NOT (relrowsecurity AND relforcerowsecurity);
   ```
   Expected rows only for `processed_events`, `outbox_*` and `pgcommon_migrations`.
5. **The vector store.** A Qdrant query built outside `corpusscope` skips the mandatory compound filter (DEP-1). Search the diff for `qdrant_client` imports outside `src/rag/corpusscope`; the architecture test should have failed.
6. **The scope cache.** A cached scope can make a purge or grant change visible up to 60 s late — that is documented, not a leak, but confirm the report is not just that window.
7. **The sweeper pool.** `rag_sweeper` can enumerate tenants by design (column-limited, read-only). Confirm it was not used for a request path.

## Resolution
- Fix the code or role, add a regression test at the failing layer (repository or e2e, as a **non-superuser**), and redeploy.
- Preserve the audit chain entries and request logs for the affected artefacts before any purge.
- Notify the platform and security owners ([`.github/SECURITY.md`](../.github/SECURITY.md)).
