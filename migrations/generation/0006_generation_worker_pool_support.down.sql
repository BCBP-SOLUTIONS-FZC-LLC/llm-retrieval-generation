-- PostgreSQL cannot drop an enum value, so the type is rebuilt without it. A row that holds the value
-- reads `internal` afterwards.
SET ROLE rag_owner;
REVOKE SELECT (status, updated_at) ON generation_runs FROM rag_sweeper;
ALTER TABLE generation_runs DROP COLUMN IF EXISTS suspended_at;
ALTER TYPE run_failure_reason RENAME TO run_failure_reason_old;
CREATE TYPE run_failure_reason AS ENUM ('scope_revoked', 'quota_exhausted', 'inference_unavailable', 'residency_capacity', 'artefact_store_unwritable', 'gate_expired', 'schema_violation', 'internal');
ALTER TABLE artefact_sections ALTER COLUMN failure_reason TYPE run_failure_reason
  USING (CASE failure_reason::text WHEN 'tenant_suspended' THEN 'internal' ELSE failure_reason::text END)::run_failure_reason;
ALTER TABLE generation_runs ALTER COLUMN failure_reason TYPE run_failure_reason
  USING (CASE failure_reason::text WHEN 'tenant_suspended' THEN 'internal' ELSE failure_reason::text END)::run_failure_reason;
DROP TYPE run_failure_reason_old;
RESET ROLE;

SET ROLE rag_owner;
REVOKE DELETE ON artefact_sections FROM rag_activity;
DROP TRIGGER IF EXISTS trg_reject_unsafe_section_delete ON artefact_sections;
DROP FUNCTION IF EXISTS reject_unsafe_section_delete();
RESET ROLE;
