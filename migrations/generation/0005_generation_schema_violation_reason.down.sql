-- PostgreSQL cannot drop an enum value, so the type is rebuilt without it. A row that holds the value
-- reads `internal` afterwards (the reason it carried before this migration).
SET ROLE rag_owner;
ALTER TYPE run_failure_reason RENAME TO run_failure_reason_old;
CREATE TYPE run_failure_reason AS ENUM ('scope_revoked', 'quota_exhausted', 'inference_unavailable', 'residency_capacity', 'artefact_store_unwritable', 'gate_expired', 'internal');
ALTER TABLE artefact_sections ALTER COLUMN failure_reason TYPE run_failure_reason
  USING (CASE failure_reason::text WHEN 'schema_violation' THEN 'internal' ELSE failure_reason::text END)::run_failure_reason;
ALTER TABLE generation_runs ALTER COLUMN failure_reason TYPE run_failure_reason
  USING (CASE failure_reason::text WHEN 'schema_violation' THEN 'internal' ELSE failure_reason::text END)::run_failure_reason;
DROP TYPE run_failure_reason_old;
RESET ROLE;
