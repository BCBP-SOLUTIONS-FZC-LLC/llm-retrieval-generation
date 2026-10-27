-- Local dev bootstrap, run once by the postgres image on first start (as POSTGRES_USER, a
-- superuser). POSTGRES_DB creates llm_corpus; the second instance (LLD §4.1) is created here.
--
-- The migrations (migrations/*/0000_roles) create the NOLOGIN group roles idempotently, so they
-- are pre-created here only to hang the login role off them. The app connects as
-- rag_app_login (a member of rag_app, never a superuser, never BYPASSRLS: a superuser
-- bypasses RLS even with FORCE). DDL runs as POSTGRES_USER, direct to Postgres.
SELECT 'CREATE DATABASE llm_generation'
WHERE NOT EXISTS (SELECT FROM pg_database WHERE datname = 'llm_generation')\gexec

DO $$
BEGIN
  IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'rag_app') THEN
    CREATE ROLE rag_app NOLOGIN;
  END IF;
  IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'rag_app_login') THEN
    CREATE ROLE rag_app_login LOGIN PASSWORD 'devpassword' IN ROLE rag_app;
  END IF;
END
$$;
