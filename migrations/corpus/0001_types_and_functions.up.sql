-- LLD 4.2 / 4.7 enums and 4.8.2 shared trigger functions. Owned by rag_owner.
SET ROLE rag_owner;

CREATE TYPE grant_level AS ENUM ('search', 'answer', 'generate');
CREATE TYPE collection_type AS ENUM ('tender', 'tenant', 'reference');
CREATE TYPE collection_status AS ENUM ('provisioning', 'active', 'frozen', 'purging', 'migration_failed', 'superseded');
CREATE TYPE index_generation_status AS ENUM ('registered', 'building', 'verified', 'active', 'failed', 'retired');
CREATE TYPE grant_subject_type AS ENUM ('user');

-- LLD 4.8.2 shared function set. Refuses an UPDATE that changes any column except those in TG_ARGV.
CREATE FUNCTION reject_update_except() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE o jsonb := to_jsonb(OLD); n jsonb := to_jsonb(NEW); k text;
BEGIN
  FOR k IN SELECT jsonb_object_keys(o) LOOP
    IF NOT (k = ANY (TG_ARGV)) AND (o -> k) IS DISTINCT FROM (n -> k) THEN
      RAISE EXCEPTION '%.% is immutable', TG_TABLE_NAME, k USING ERRCODE = '23514';
    END IF;
  END LOOP;
  RETURN NEW;
END $$;

-- Refuses an UPDATE that changes any column named in TG_ARGV.
CREATE FUNCTION reject_column_change() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE o jsonb := to_jsonb(OLD); n jsonb := to_jsonb(NEW); k text;
BEGIN
  FOREACH k IN ARRAY TG_ARGV LOOP
    IF (o -> k) IS DISTINCT FROM (n -> k) THEN
      RAISE EXCEPTION '%.% is fixed at insert', TG_TABLE_NAME, k USING ERRCODE = '23514';
    END IF;
  END LOOP;
  RETURN NEW;
END $$;

-- Refuses the statement outright.
CREATE FUNCTION reject_always() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  RAISE EXCEPTION '% is append-only; % refused', TG_TABLE_NAME, TG_OP USING ERRCODE = '23514';
END $$;

CREATE FUNCTION touch_updated_at() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF NEW IS NOT DISTINCT FROM OLD THEN RETURN OLD; END IF;
  NEW.updated_at := now(); RETURN NEW;
END $$;

-- Server side of optimistic concurrency (LLD 14.1): the caller's expected value is a WHERE
-- predicate, and the new value is never the caller's to supply.
CREATE FUNCTION bump_record_version() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF NEW IS NOT DISTINCT FROM OLD THEN RETURN OLD; END IF;
  NEW.record_version := OLD.record_version + 1; RETURN NEW;
END $$;

RESET ROLE;
