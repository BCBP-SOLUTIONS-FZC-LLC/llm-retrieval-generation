-- LLD 4.2 / 4.7 enums and 4.8.2 shared trigger functions. Owned by rag_owner.
CREATE EXTENSION IF NOT EXISTS pgcrypto;   -- digest() for the audit chain's entry_hash (AC-2)
SET ROLE rag_owner;

CREATE TYPE evidence_tier AS ENUM ('NOT_COMPUTED', 'INSUFFICIENT', 'PARTIAL', 'SUPPORTED');
CREATE TYPE grant_level AS ENUM ('search', 'answer', 'generate');
CREATE TYPE artefact_kind AS ENUM ('answer', 'section_draft', 'document', 'extraction');
CREATE TYPE artefact_status AS ENUM ('pending', 'generating', 'complete', 'cancelled', 'failed', 'superseded');
CREATE TYPE artefact_archive_state AS ENUM ('live', 'archived');
CREATE TYPE audit_actor_type AS ENUM ('user', 'service_account', 'iam_system', 'anonymous');
CREATE TYPE artefact_origin AS ENUM ('original', 'regeneration', 'coherence_repair');
CREATE TYPE delivery_state AS ENUM ('delivered', 'disconnected', 'not_applicable');
CREATE TYPE eligibility_state AS ENUM ('GENERATABLE', 'PARTIALLY_GENERATABLE', 'NOT_GENERATABLE');
CREATE TYPE generation_mode AS ENUM ('assistance', 'automation');
CREATE TYPE retrieval_verdict AS ENUM ('sufficient', 'insufficient', 'failed');
CREATE TYPE completeness_verdict AS ENUM ('complete', 'partial', 'insufficient', 'not_computed');
CREATE TYPE claim_outcome AS ENUM ('supported', 'unsupported', 'miscited', 'gap');
CREATE TYPE section_state AS ENUM ('pending', 'generating', 'complete', 'gap', 'failed');
CREATE TYPE refiner_outcome AS ENUM ('success', 'degraded', 'disabled');
CREATE TYPE user_action AS ENUM ('accepted', 'edited', 'discarded', 'unknown');
CREATE TYPE instruction_type AS ENUM ('style', 'add', 'remove', 'correct', 'expand');
CREATE TYPE conflict_resolution AS ENUM ('keep_established', 'update_fact', 'dismiss');
CREATE TYPE run_status AS ENUM ('pending', 'outline_building', 'awaiting_outline_approval', 'generating', 'suspended', 'complete', 'failed', 'cancelled');
CREATE TYPE run_failure_reason AS ENUM ('scope_revoked', 'quota_exhausted', 'inference_unavailable', 'residency_capacity', 'artefact_store_unwritable', 'gate_expired', 'internal');
CREATE TYPE outline_state AS ENUM ('none', 'building', 'ready', 'approved', 'revising');
CREATE TYPE run_stage AS ENUM ('outline', 'stage1', 'stage2', 'coherence', 'extract');
CREATE TYPE coherence_verdict AS ENUM ('not_computed', 'consistent', 'conflicted');

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
