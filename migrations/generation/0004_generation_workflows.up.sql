-- Workflows slice (LLD 4.3.6, RUN-2, 5.7.9): a run's workflow_id is unique per tenant while set, so a
-- repeated A-5 from one base version (workflow_id regen:{base_version_id}) can never insert a second run
-- row under one Temporal id. An original's id is its own artefact id, so it never collides either.
SET ROLE rag_owner;
CREATE UNIQUE INDEX uq_run_workflow ON generation_runs (tenant_id, workflow_id) WHERE workflow_id IS NOT NULL;
-- A-6 / A-7 look up a section's conflicts (the one already resolved keep_established is not raised again).
CREATE INDEX idx_fc_section ON fact_conflicts (tenant_id, artefact_id, section_id);
RESET ROLE;
