-- R-4 (5.7.4): a model answer that breaks the resolved schema closes the run with its own reason, no
-- longer `internal`. run_failure_reason is also the `reason` of llm.generation.failed (4.2), so a
-- consumer that branches on it sees one new value (recorded in docs/implementation-plan.md).
SET ROLE rag_owner;
ALTER TYPE run_failure_reason ADD VALUE IF NOT EXISTS 'schema_violation' BEFORE 'internal';
RESET ROLE;
