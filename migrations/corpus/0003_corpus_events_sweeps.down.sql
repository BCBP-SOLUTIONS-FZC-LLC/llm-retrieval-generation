REVOKE USAGE ON SEQUENCE outbox_events_ordering_seq FROM rag_app;
REVOKE SELECT, INSERT, UPDATE, DELETE ON outbox_events, outbox_dead_letters FROM rag_app;

SET ROLE rag_owner;
DROP POLICY IF EXISTS sweep_enumerate ON collections;
DROP POLICY IF EXISTS sweep_enumerate ON tenant_lifecycle;
REVOKE SELECT (tenant_id, status, promoted_from, created_at) ON collections FROM rag_sweeper;
REVOKE SELECT (tenant_id, state, changed_at) ON tenant_lifecycle FROM rag_sweeper;
RESET ROLE;
REVOKE USAGE ON SCHEMA public FROM rag_sweeper;
