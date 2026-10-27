ALTER DEFAULT PRIVILEGES FOR ROLE rag_owner IN SCHEMA public REVOKE SELECT, INSERT, UPDATE ON TABLES FROM rag_app;
REVOKE CREATE, USAGE ON SCHEMA public FROM rag_owner;
REVOKE USAGE ON SCHEMA public FROM rag_app, rag_activity;
-- Roles and rag_owner membership are cluster-global and may serve the other instance: kept.
