-- Worker Pool support (llm-workers LLD review, P0-1 and P0-6, G-3):
--  * `tenant_suspended`: a run held because its tenant is frozen, distinct from `quota_exhausted`.
--    run_failure_reason is also the `reason` of llm.generation.failed and llm.generation.progressed,
--    so a consumer that branches on it sees one new value (recorded in docs/implementation-plan.md).
--  * `suspended_at`: when the run entered `suspended`, so the 30-day deadline is read from the row
--    and survives a W-8 restart, continue-as-new and a worker change. Written by `suspend_for_quota`,
--    cleared on resume.
--  * rag_sweeper may read the two columns the `quiet-async-runs` sweep enumerates tenants by. Not
--    `workflow_id`: it embeds artefact and version ids, and the tenant-scoped sweep filters on it.
SET ROLE rag_owner;
ALTER TYPE run_failure_reason ADD VALUE IF NOT EXISTS 'tenant_suspended' BEFORE 'internal';
ALTER TABLE generation_runs ADD COLUMN IF NOT EXISTS suspended_at timestamptz;
GRANT SELECT (status, updated_at) ON generation_runs TO rag_sweeper;
RESET ROLE;

-- Outline revision (workers LLD §6.1, W-4): the outline activity rewrites the section rows of a
-- revised outline, so rag_activity may DELETE a section that has not started. Nothing else: a
-- section past `pending`, or holding a version, is refused for any member of rag_activity.
SET ROLE rag_owner;
CREATE FUNCTION reject_unsafe_section_delete() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF pg_has_role(current_user, 'rag_activity', 'MEMBER')
     AND (OLD.state <> 'pending' OR OLD.current_version_id IS NOT NULL) THEN
    RAISE EXCEPTION 'a worker may delete only a pending section without a version'
      USING ERRCODE = 'check_violation';
  END IF;
  RETURN OLD;
END $$;
CREATE TRIGGER trg_reject_unsafe_section_delete BEFORE DELETE ON artefact_sections
  FOR EACH ROW EXECUTE FUNCTION reject_unsafe_section_delete();
GRANT DELETE ON artefact_sections TO rag_activity;
RESET ROLE;
