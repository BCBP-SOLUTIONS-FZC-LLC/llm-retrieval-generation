SET ROLE rag_owner;
ALTER TABLE artefact_audit_chain ALTER COLUMN source_service SET DEFAULT 'llm-rag-service';
RESET ROLE;
