-- LLD 4.1 roles. NOLOGIN group roles; login roles are granted membership by the platform (OQ-67).
-- Roles are cluster-global, so creation is idempotent (both instances may share a cluster).
DO $$
BEGIN
  IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'rag_app') THEN CREATE ROLE rag_app NOLOGIN; END IF;
  IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'rag_activity') THEN CREATE ROLE rag_activity NOLOGIN; END IF;
  IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'rag_owner') THEN CREATE ROLE rag_owner NOLOGIN; END IF;
END $$;

-- rag_owner owns every table; nothing logs in as it. The migration role may SET ROLE to it so
-- that DDL migrations (0001+) create objects owned by rag_owner. FORCE RLS then binds the owner.
GRANT rag_owner TO CURRENT_USER;
GRANT USAGE ON SCHEMA public TO rag_app, rag_activity;
GRANT CREATE, USAGE ON SCHEMA public TO rag_owner;

-- Tables are created by rag_owner, so default privileges give rag_app the LLD 4.1 baseline
-- (SELECT, INSERT, UPDATE on every table). No role is BYPASSRLS.
ALTER DEFAULT PRIVILEGES FOR ROLE rag_owner IN SCHEMA public GRANT SELECT, INSERT, UPDATE ON TABLES TO rag_app;
-- rag_activity's per-table grants (LLD 4.1) are issued with each table (0002+): GRANT needs the table.
