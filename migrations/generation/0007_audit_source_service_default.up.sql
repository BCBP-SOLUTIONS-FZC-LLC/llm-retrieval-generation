-- The event and audit source is `llm-retrieval-generation`, the repository's own name (it was
-- `llm-rag-service`). Only the column default changes: rows already written keep the value they were
-- hashed with, because the audit chain is append-only and `source_service` is part of each row's hash.
SET ROLE rag_owner;
ALTER TABLE artefact_audit_chain ALTER COLUMN source_service SET DEFAULT 'llm-retrieval-generation';
RESET ROLE;
