SET ROLE rag_owner;
DROP TABLE IF EXISTS
  artefact_audit_chain, instruction_refinements, query_refinements,
  graph_checkpoints, fact_conflicts, document_facts, completeness_checks, claim_verifications,
  groundedness_reports, retrieval_set_purges, retrieval_set_chunks, artefact_version_sections,
  artefact_section_versions, artefact_sections, artefact_versions, generation_runs,
  retrieval_sets, artefacts CASCADE;
DROP FUNCTION IF EXISTS audit_chain_append();
DROP FUNCTION IF EXISTS audit_canonical_json(jsonb);
DROP FUNCTION IF EXISTS reject_section_transition();
RESET ROLE;
