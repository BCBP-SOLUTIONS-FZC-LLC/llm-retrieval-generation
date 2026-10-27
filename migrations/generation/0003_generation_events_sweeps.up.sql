-- Events slice (LLD 4.6.4, 21.2): outbox grants and the sweeps' tenant enumeration on llm_generation.
-- Needs eventcommon's outbox schema to exist first: migrate_all applies it BEFORE this runner.

DO $$
BEGIN
  IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'rag_sweeper') THEN
    CREATE ROLE rag_sweeper NOLOGIN;
  END IF;
END $$;

-- 1. rag_sweeper: tenant ids with candidate rows, nothing else (no BYPASSRLS, column grants,
--    a FOR SELECT policy naming this role alone). Writes happen as rag_app under the tenant's GUC.
GRANT USAGE ON SCHEMA public TO rag_sweeper;

SET ROLE rag_owner;
GRANT SELECT (tenant_id, status, created_at, updated_at) ON artefacts TO rag_sweeper;
GRANT SELECT (tenant_id, idempotency_key, created_at) ON generation_runs TO rag_sweeper;
GRANT SELECT (tenant_id, artefact_id, created_at) ON retrieval_sets TO rag_sweeper;
GRANT SELECT (tenant_id, created_at) ON graph_checkpoints TO rag_sweeper;
GRANT SELECT (tenant_id, created_at) ON artefact_audit_chain TO rag_sweeper;
DO $$
DECLARE t text;
BEGIN
  FOREACH t IN ARRAY ARRAY['artefacts', 'generation_runs', 'retrieval_sets', 'graph_checkpoints',
                           'artefact_audit_chain']
  LOOP
    EXECUTE format('CREATE POLICY sweep_enumerate ON %I FOR SELECT TO rag_sweeper USING (true)', t);
  END LOOP;
END $$;
RESET ROLE;

-- 2. The outbox is eventcommon's (OBX-4) and owned by the migration role: grants outside SET ROLE.
--    rag_app: the relay (claim, mark, prune, dead-letter) and every in-service enqueue.
--    rag_activity: the Worker Pool's activities enqueue onto this one table and run no relay
--    (OBX-3), so INSERT and the ordering sequence only.
GRANT SELECT, INSERT, UPDATE, DELETE ON outbox_events, outbox_dead_letters TO rag_app;
GRANT USAGE ON SEQUENCE outbox_events_ordering_seq TO rag_app, rag_activity;
GRANT INSERT ON outbox_events TO rag_activity;

-- 3. The consumer-ledger sweep (21.2, PE-1, EVT-16) prunes processed_events as rag_app, and 0002
--    granted that table no DELETE (the baseline is SELECT, INSERT, UPDATE).
GRANT DELETE ON processed_events TO rag_app;
