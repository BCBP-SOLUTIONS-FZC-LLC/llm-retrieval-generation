-- Events slice (LLD 4.6.4, 21.2): outbox grants and the sweeps' tenant enumeration on llm_corpus.
-- Needs eventcommon's outbox schema to exist first: migrate_all applies it BEFORE this runner.

DO $$
BEGIN
  IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'rag_sweeper') THEN
    CREATE ROLE rag_sweeper NOLOGIN;
  END IF;
END $$;

-- 1. rag_sweeper: the one non-tenant read path, column-limited and policy-limited (no BYPASSRLS).
GRANT USAGE ON SCHEMA public TO rag_sweeper;

SET ROLE rag_owner;
GRANT SELECT (tenant_id, state, changed_at) ON tenant_lifecycle TO rag_sweeper;
GRANT SELECT (tenant_id, status, promoted_from, created_at) ON collections TO rag_sweeper;
CREATE POLICY sweep_enumerate ON tenant_lifecycle FOR SELECT TO rag_sweeper USING (true);
CREATE POLICY sweep_enumerate ON collections FOR SELECT TO rag_sweeper USING (true);
RESET ROLE;

-- 2. The outbox is eventcommon's (OBX-4) and owned by the migration role, so these grants are
--    issued outside SET ROLE. The relay (rag_app) claims, marks, deletes and dead-letters; the
--    corpus use cases enqueue in their own transaction (EVT-8).
GRANT SELECT, INSERT, UPDATE, DELETE ON outbox_events, outbox_dead_letters TO rag_app;
GRANT USAGE ON SEQUENCE outbox_events_ordering_seq TO rag_app;
