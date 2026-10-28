REVOKE SELECT, INSERT, UPDATE, DELETE ON processed_events FROM rag_app;
REVOKE INSERT ON outbox_events FROM rag_activity;
REVOKE USAGE ON SEQUENCE outbox_events_ordering_seq FROM rag_app, rag_activity;
REVOKE SELECT, INSERT, UPDATE, DELETE ON outbox_events, outbox_dead_letters FROM rag_app;

SET ROLE rag_owner;
DROP POLICY IF EXISTS sweep_enumerate ON artefact_audit_chain;
DROP POLICY IF EXISTS sweep_enumerate ON graph_checkpoints;
DROP POLICY IF EXISTS sweep_enumerate ON retrieval_sets;
DROP POLICY IF EXISTS sweep_enumerate ON generation_runs;
DROP POLICY IF EXISTS sweep_enumerate ON artefacts;
REVOKE SELECT (tenant_id, created_at) ON artefact_audit_chain FROM rag_sweeper;
REVOKE SELECT (tenant_id, created_at) ON graph_checkpoints FROM rag_sweeper;
REVOKE SELECT (tenant_id, artefact_id, created_at) ON retrieval_sets FROM rag_sweeper;
REVOKE SELECT (tenant_id, idempotency_key, created_at) ON generation_runs FROM rag_sweeper;
REVOKE SELECT (tenant_id, status, created_at, updated_at) ON artefacts FROM rag_sweeper;
RESET ROLE;
REVOKE USAGE ON SCHEMA public FROM rag_sweeper;
