SET ROLE rag_owner;
DROP TABLE IF EXISTS tenant_lifecycle;
DROP TABLE IF EXISTS retention_policies;
DROP TABLE IF EXISTS index_generations;
DROP TABLE IF EXISTS collection_grants;
DROP TABLE IF EXISTS collections;
DROP FUNCTION IF EXISTS reject_generation_transition();
DROP FUNCTION IF EXISTS reject_purged_change();
RESET ROLE;
