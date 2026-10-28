-- LLD 4.3-4.6, 4.8: every llm_generation table, cross-table FKs, indexes, triggers, RLS, grants.
-- outbox_events / outbox_dead_letters are eventcommon's (OBX-4) and are not created here.
SET ROLE rag_owner;

-- ---------------------------------------------------------------------------------------
-- 1. Tables (no cross-table foreign keys; those follow in section 2)
-- ---------------------------------------------------------------------------------------
CREATE TABLE artefacts (
  id                 uuid PRIMARY KEY DEFAULT uuidv7(),
  tenant_id          uuid NOT NULL,
  tender_id          uuid,
  kind               artefact_kind NOT NULL,
  capability         text NOT NULL,
  tender_section_ref text,
  target_ref         text,
  status             artefact_status NOT NULL DEFAULT 'pending',
  current_version_id uuid,
  created_by         uuid NOT NULL,
  archive_state      artefact_archive_state NOT NULL DEFAULT 'live',
  archived_at        timestamptz,
  record_version     bigint NOT NULL DEFAULT 1 CHECK (record_version > 0),
  created_at         timestamptz NOT NULL DEFAULT now(),
  updated_at         timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT ck_artefact_tender CHECK (tender_id IS NOT NULL OR kind = 'answer'),
  CONSTRAINT ck_artefact_archive CHECK (archive_state = 'live' OR (archived_at IS NOT NULL AND tender_id IS NOT NULL))
);

CREATE TABLE artefact_versions (
  id                        uuid PRIMARY KEY DEFAULT uuidv7(),
  artefact_id               uuid NOT NULL,
  tenant_id                 uuid NOT NULL,
  version_no                int NOT NULL CHECK (version_no > 0),
  minted_reason             text NOT NULL,
  origin                    artefact_origin NOT NULL DEFAULT 'original',
  evidence_tier             evidence_tier NOT NULL,
  completeness              completeness_verdict NOT NULL,
  superseded_by_section_edit boolean NOT NULL DEFAULT false,
  eligibility_state         eligibility_state NOT NULL,
  requested_mode            generation_mode NOT NULL,
  effective_mode            generation_mode NOT NULL,
  clamped                   boolean NOT NULL DEFAULT false,
  clamp_reason              text,
  domain_profile_version    text NOT NULL,
  language_pack_version     text NOT NULL,
  model_profile_id          text NOT NULL,
  prompt_hash               text NOT NULL,
  parser_version            text NOT NULL,
  chunker_version           text NOT NULL,
  generation_run_id         uuid NOT NULL,
  output_schema_version     text,
  delivery                  delivery_state NOT NULL,
  degradation_markers       jsonb NOT NULL DEFAULT '[]',
  instruction               text,
  instruction_refinement_id uuid,
  review_context_read       boolean NOT NULL DEFAULT false,
  review_context_ref        text,
  disclaimer_ref            text,
  created_at                timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT ck_av_clamp CHECK (clamped = false OR clamp_reason IS NOT NULL)
);

CREATE TABLE artefact_version_sections (
  artefact_version_id uuid NOT NULL,
  section_version_id  uuid NOT NULL,
  tenant_id           uuid NOT NULL,
  PRIMARY KEY (artefact_version_id, section_version_id)
);

CREATE TABLE artefact_sections (
  id                 uuid PRIMARY KEY DEFAULT uuidv7(),
  artefact_id        uuid NOT NULL,
  tenant_id          uuid NOT NULL,
  tender_section_ref text,
  section_type       text NOT NULL,
  ordinal            int NOT NULL,
  is_foundation      boolean NOT NULL DEFAULT false,
  state              section_state NOT NULL DEFAULT 'pending',
  gap_reason         text,
  failure_reason     run_failure_reason,
  current_version_id uuid,
  flagged_for_review boolean NOT NULL DEFAULT false,
  flagged_reason     text,
  record_version     bigint NOT NULL DEFAULT 1 CHECK (record_version > 0),
  created_at         timestamptz NOT NULL DEFAULT now(),
  updated_at         timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT ck_as_gap CHECK (state <> 'gap' OR gap_reason IS NOT NULL),
  CONSTRAINT ck_as_failed CHECK (state <> 'failed' OR failure_reason IS NOT NULL)
);

CREATE TABLE artefact_section_versions (
  id                  uuid PRIMARY KEY DEFAULT uuidv7(),
  section_id          uuid NOT NULL,
  tenant_id           uuid NOT NULL,
  version_no          int NOT NULL CHECK (version_no > 0),
  origin              artefact_origin NOT NULL DEFAULT 'original',
  content_ref         text NOT NULL,
  content_sha256      bytea NOT NULL,
  retrieval_set_id    uuid NOT NULL,
  evidence_tier       evidence_tier NOT NULL,
  completeness        completeness_verdict NOT NULL,
  scope_narrowed      boolean NOT NULL DEFAULT false,
  model_profile_id    text NOT NULL,
  prompt_hash         text NOT NULL,
  serving_tier        text NOT NULL,
  auto_retry_count    int NOT NULL DEFAULT 0 CHECK (auto_retry_count <= 2),
  manual_regen_count  int NOT NULL DEFAULT 0,
  degradation_markers jsonb NOT NULL DEFAULT '[]',
  created_at          timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE generation_runs (
  id                     uuid PRIMARY KEY DEFAULT uuidv7(),
  artefact_id            uuid NOT NULL,
  tenant_id              uuid NOT NULL,
  tender_id              uuid,
  principal_id           uuid NOT NULL,
  origin                 artefact_origin NOT NULL DEFAULT 'original',
  base_version_id        uuid,
  workflow_id            text,
  run_input              jsonb,
  current_stage          run_stage,
  idempotency_key        text,
  request_hash           text,
  status                 run_status NOT NULL DEFAULT 'pending',
  failure_reason         run_failure_reason,
  failed_at_stage        run_stage,
  requested_mode         generation_mode NOT NULL,
  effective_mode         generation_mode NOT NULL,
  eligibility_state      eligibility_state NOT NULL,
  clamped                boolean NOT NULL DEFAULT false,
  clamp_reason           text,
  domain_profile_version text NOT NULL,
  language_pack_version  text NOT NULL,
  outline_state          outline_state NOT NULL DEFAULT 'none',
  outline_version        int,
  outline_ready_at       timestamptz,
  gate_expires_at        timestamptz,
  coherence              coherence_verdict NOT NULL DEFAULT 'not_computed',
  coherence_computed_at  timestamptz,
  record_version         bigint NOT NULL DEFAULT 1 CHECK (record_version > 0),
  created_at             timestamptz NOT NULL DEFAULT now(),
  updated_at             timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT ck_run_clamp   CHECK (clamped = false OR clamp_reason IS NOT NULL),
  CONSTRAINT ck_run_failure CHECK (status <> 'failed' OR failure_reason IS NOT NULL),
  CONSTRAINT ck_run_base    CHECK (origin = 'original' OR base_version_id IS NOT NULL),
  CONSTRAINT ck_run_idem    CHECK (idempotency_key IS NULL OR request_hash IS NOT NULL),
  CONSTRAINT ck_run_outline CHECK (outline_state = 'none' OR outline_version IS NOT NULL)
);

CREATE TABLE retrieval_sets (
  id                     uuid PRIMARY KEY DEFAULT uuidv7(),
  tenant_id              uuid NOT NULL,
  tender_id              uuid,
  artefact_id            uuid,
  verdict                retrieval_verdict NOT NULL,
  verdict_reason         text,
  scope_resolution_hash  text NOT NULL,
  grant_level_applied    grant_level NOT NULL,
  grant_level_clamped    boolean NOT NULL DEFAULT false,
  filter_set             jsonb NOT NULL,
  index_generations      jsonb NOT NULL,
  profile_source         text NOT NULL,
  applied_profile        jsonb NOT NULL,
  values_provisional     boolean NOT NULL DEFAULT false,
  reranker_ran           boolean NOT NULL,
  budget_allocation      jsonb NOT NULL,
  query_refinement_id    uuid,
  widened_retry_of       uuid,
  domain_profile_version text NOT NULL,
  language_pack_version  text NOT NULL,
  degradation_markers    jsonb NOT NULL DEFAULT '[]',
  created_at             timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT ck_rs_verdict CHECK (verdict = 'sufficient' OR verdict_reason IS NOT NULL)
);

CREATE TABLE retrieval_set_chunks (
  retrieval_set_id     uuid NOT NULL,
  tenant_id            uuid NOT NULL,
  rank                 int NOT NULL,
  chunk_id             text NOT NULL,
  document_id          uuid NOT NULL,
  document_version     int NOT NULL,
  collection_id        uuid NOT NULL,
  collection_weight    numeric(3,2) NOT NULL,
  fused_score          numeric NOT NULL,
  rerank_score         numeric,
  source_half          text NOT NULL,
  parent_chunk_id      text,
  document_title       text NOT NULL,
  heading_path         text,
  clause_ref           text,
  document_role        text NOT NULL,
  language             text NOT NULL,
  excerpt              text NOT NULL,
  excerpt_is_translation boolean NOT NULL DEFAULT false,
  source_span          jsonb,
  PRIMARY KEY (retrieval_set_id, rank)
);

CREATE TABLE retrieval_set_purges (
  id                 uuid PRIMARY KEY DEFAULT uuidv7(),
  retrieval_set_id   uuid NOT NULL,
  tenant_id          uuid NOT NULL,
  purged_document_id uuid NOT NULL,
  purged_version     int NOT NULL,
  chunk_ids          text[] NOT NULL,
  event_id           uuid NOT NULL,
  purged_at          timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE groundedness_reports (
  id                     uuid PRIMARY KEY DEFAULT uuidv7(),
  section_version_id     uuid NOT NULL,
  tenant_id              uuid NOT NULL,
  tiers_run              int[] NOT NULL,
  claims_total           int NOT NULL,
  supported              int NOT NULL,
  unsupported            int NOT NULL,
  miscited               int NOT NULL,
  gaps                   int NOT NULL,
  score                  numeric(4,3),
  evidence_tier          evidence_tier NOT NULL,
  judge_model_profile_id text,
  created_at             timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT ck_gr_totals CHECK (claims_total = supported + unsupported + miscited + gaps)
);

CREATE TABLE claim_verifications (
  report_id           uuid NOT NULL,
  claim_ordinal       int NOT NULL,
  tenant_id           uuid NOT NULL,
  outcome             claim_outcome NOT NULL,
  cited_chunk_ids     text[] NOT NULL,
  corrected_chunk_ids text[],
  tier_decided        int NOT NULL,
  PRIMARY KEY (report_id, claim_ordinal)
);

CREATE TABLE completeness_checks (
  id                 uuid PRIMARY KEY DEFAULT uuidv7(),
  section_version_id uuid NOT NULL,
  tenant_id          uuid NOT NULL,
  verdict            completeness_verdict NOT NULL,
  items_total        int NOT NULL,
  items_addressed    int NOT NULL,
  missed_items       text[] NOT NULL DEFAULT '{}',
  created_at         timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT ck_cc_addressed CHECK (items_addressed <= items_total)
);

CREATE TABLE document_facts (
  id                        uuid PRIMARY KEY DEFAULT uuidv7(),
  artefact_id               uuid NOT NULL,
  tenant_id                 uuid NOT NULL,
  fact_key                  text NOT NULL,
  value                     text NOT NULL,
  established_by_section_id uuid NOT NULL,
  superseded_at             timestamptz,
  created_at                timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE fact_conflicts (
  id                        uuid PRIMARY KEY DEFAULT uuidv7(),
  tenant_id                 uuid NOT NULL,
  artefact_id               uuid NOT NULL,
  section_id                uuid NOT NULL,
  fact_key                  text NOT NULL,
  established_value         text NOT NULL,
  instruction_value         text NOT NULL,
  established_by_section_id uuid NOT NULL,
  resolution                conflict_resolution,
  resolved_at               timestamptz,
  record_version            bigint NOT NULL DEFAULT 1 CHECK (record_version > 0),
  created_at                timestamptz NOT NULL DEFAULT now(),
  updated_at                timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT ck_fc_resolved CHECK ((resolution IS NULL) = (resolved_at IS NULL))
);

CREATE TABLE graph_checkpoints (
  id              uuid PRIMARY KEY DEFAULT uuidv7(),
  tenant_id       uuid NOT NULL,
  section_id      uuid NOT NULL,
  activity_run_id text NOT NULL,
  state           jsonb NOT NULL,
  created_at      timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE query_refinements (
  id               uuid PRIMARY KEY DEFAULT uuidv7(),
  tenant_id        uuid NOT NULL,
  retrieval_set_id uuid,
  principal_hash   text NOT NULL,
  original_query   text NOT NULL,
  refined_query    text,
  intent           text,
  exact_terms      text[],
  preferred_roles  text[],
  section_type     text,
  pack_version     text NOT NULL,
  refiner_version  text NOT NULL,
  latency_ms       int NOT NULL,
  outcome          refiner_outcome NOT NULL,
  user_action      user_action NOT NULL DEFAULT 'unknown',
  downstream       jsonb,
  created_at       timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE instruction_refinements (
  id                   uuid PRIMARY KEY DEFAULT uuidv7(),
  tenant_id            uuid NOT NULL,
  artefact_version_id  uuid,
  section_id           uuid,
  principal_hash       text NOT NULL,
  original_instruction text NOT NULL,
  refined_instruction  text,
  instruction_type     instruction_type,
  needs_retrieval      boolean NOT NULL DEFAULT true,
  fact_conflict_id     uuid,
  conflict_resolution  conflict_resolution,
  pack_version         text NOT NULL,
  refiner_version      text NOT NULL,
  latency_ms           int NOT NULL,
  outcome              refiner_outcome NOT NULL,
  user_action          user_action NOT NULL DEFAULT 'unknown',
  created_at           timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE artefact_audit_chain (
  tenant_id         uuid NOT NULL,
  seq               bigint NOT NULL,
  occurred_at       timestamptz NOT NULL,
  entry_type        text NOT NULL,
  action            text NOT NULL,
  actor_type        audit_actor_type NOT NULL,
  actor_id          uuid,
  artefact_id       uuid NOT NULL,
  target_type       text,
  target_id         text,
  source_service    text NOT NULL DEFAULT 'llm-rag-service',
  source_event_type text NOT NULL,
  source_event_id   text NOT NULL,
  trace_id          text,
  metadata          jsonb NOT NULL DEFAULT '{}',
  payload_hash      bytea NOT NULL,
  prev_hash         bytea,
  entry_hash        bytea NOT NULL,
  created_at        timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (tenant_id, seq),
  CONSTRAINT chk_audit_actor CHECK ((actor_type = 'anonymous' AND actor_id IS NULL) OR (actor_type <> 'anonymous' AND actor_id IS NOT NULL)),
  CONSTRAINT chk_audit_metadata CHECK (jsonb_typeof(metadata) = 'object' AND octet_length(metadata::text) <= 8192)
);

-- processed_events (the consumers' dedup ledger) is eventcommon's inbox, not ours: migrate_all applies
-- eventcommon.inbox.apply_schema before this runner, exactly as it does for the outbox (OBX-4, PE-1).

-- ---------------------------------------------------------------------------------------
-- 2. Cross-table foreign keys (ART-2, AS-3, ART-9 cycles are DEFERRABLE as specified)
-- ---------------------------------------------------------------------------------------
ALTER TABLE artefacts ADD CONSTRAINT fk_artefact_current_version
  FOREIGN KEY (current_version_id) REFERENCES artefact_versions(id) DEFERRABLE INITIALLY DEFERRED;

ALTER TABLE artefact_versions
  ADD CONSTRAINT fk_av_artefact FOREIGN KEY (artefact_id) REFERENCES artefacts(id),
  ADD CONSTRAINT fk_av_generation_run FOREIGN KEY (generation_run_id) REFERENCES generation_runs(id)
    DEFERRABLE INITIALLY IMMEDIATE;

ALTER TABLE artefact_version_sections
  ADD CONSTRAINT fk_avs_version FOREIGN KEY (artefact_version_id) REFERENCES artefact_versions(id),
  ADD CONSTRAINT fk_avs_section_version FOREIGN KEY (section_version_id) REFERENCES artefact_section_versions(id);

ALTER TABLE artefact_sections
  ADD CONSTRAINT fk_as_artefact FOREIGN KEY (artefact_id) REFERENCES artefacts(id),
  ADD CONSTRAINT fk_as_current_version FOREIGN KEY (current_version_id) REFERENCES artefact_section_versions(id)
    DEFERRABLE INITIALLY DEFERRED;

ALTER TABLE artefact_section_versions
  ADD CONSTRAINT fk_asv_section FOREIGN KEY (section_id) REFERENCES artefact_sections(id),
  ADD CONSTRAINT fk_asv_retrieval_set FOREIGN KEY (retrieval_set_id) REFERENCES retrieval_sets(id);

ALTER TABLE generation_runs
  ADD CONSTRAINT fk_run_artefact FOREIGN KEY (artefact_id) REFERENCES artefacts(id),
  ADD CONSTRAINT fk_run_base_version FOREIGN KEY (base_version_id) REFERENCES artefact_versions(id)
    DEFERRABLE INITIALLY IMMEDIATE;

ALTER TABLE retrieval_sets
  ADD CONSTRAINT fk_rs_artefact FOREIGN KEY (artefact_id) REFERENCES artefacts(id),
  ADD CONSTRAINT fk_rs_widened_retry FOREIGN KEY (widened_retry_of) REFERENCES retrieval_sets(id);

ALTER TABLE retrieval_set_chunks
  ADD CONSTRAINT fk_rsc_set FOREIGN KEY (retrieval_set_id) REFERENCES retrieval_sets(id);
ALTER TABLE retrieval_set_purges
  ADD CONSTRAINT fk_rsp_set FOREIGN KEY (retrieval_set_id) REFERENCES retrieval_sets(id);
ALTER TABLE groundedness_reports
  ADD CONSTRAINT fk_gr_section_version FOREIGN KEY (section_version_id) REFERENCES artefact_section_versions(id);
ALTER TABLE claim_verifications
  ADD CONSTRAINT fk_cv_report FOREIGN KEY (report_id) REFERENCES groundedness_reports(id);
ALTER TABLE completeness_checks
  ADD CONSTRAINT fk_cc_section_version FOREIGN KEY (section_version_id) REFERENCES artefact_section_versions(id);
ALTER TABLE document_facts
  ADD CONSTRAINT fk_df_artefact FOREIGN KEY (artefact_id) REFERENCES artefacts(id),
  ADD CONSTRAINT fk_df_section FOREIGN KEY (established_by_section_id) REFERENCES artefact_sections(id);
ALTER TABLE fact_conflicts
  ADD CONSTRAINT fk_fc_artefact FOREIGN KEY (artefact_id) REFERENCES artefacts(id),
  ADD CONSTRAINT fk_fc_section FOREIGN KEY (section_id) REFERENCES artefact_sections(id),
  ADD CONSTRAINT fk_fc_established_by FOREIGN KEY (established_by_section_id) REFERENCES artefact_sections(id);
ALTER TABLE graph_checkpoints
  ADD CONSTRAINT fk_gc_section FOREIGN KEY (section_id) REFERENCES artefact_sections(id);
ALTER TABLE query_refinements
  ADD CONSTRAINT fk_qr_set FOREIGN KEY (retrieval_set_id) REFERENCES retrieval_sets(id);
ALTER TABLE instruction_refinements
  ADD CONSTRAINT fk_ir_version FOREIGN KEY (artefact_version_id) REFERENCES artefact_versions(id),
  ADD CONSTRAINT fk_ir_section FOREIGN KEY (section_id) REFERENCES artefact_sections(id),
  ADD CONSTRAINT fk_ir_conflict FOREIGN KEY (fact_conflict_id) REFERENCES fact_conflicts(id);
-- AC-6: artefact_audit_chain.artefact_id deliberately has no foreign key.

-- ---------------------------------------------------------------------------------------
-- 3. Indexes (every one leads on tenant_id except where the key is already the table's own)
-- ---------------------------------------------------------------------------------------
CREATE INDEX idx_artefacts_tenant_tender ON artefacts (tenant_id, tender_id, created_at DESC);
CREATE INDEX idx_artefacts_section ON artefacts (tenant_id, tender_section_ref) WHERE tender_section_ref IS NOT NULL;
CREATE INDEX idx_artefacts_pending ON artefacts (tenant_id, created_at) WHERE status = 'pending';
CREATE INDEX idx_artefacts_generating ON artefacts (tenant_id, updated_at) WHERE status = 'generating';

CREATE UNIQUE INDEX uq_artefact_version ON artefact_versions (artefact_id, version_no);
CREATE INDEX idx_av_tenant ON artefact_versions (tenant_id, artefact_id, version_no DESC);

CREATE UNIQUE INDEX uq_section_ordinal ON artefact_sections (artefact_id, ordinal);
CREATE INDEX idx_a4_read ON artefact_sections (tenant_id, artefact_id, ordinal)
  INCLUDE (state, current_version_id, flagged_for_review);

CREATE UNIQUE INDEX uq_section_version ON artefact_section_versions (section_id, version_no);
CREATE INDEX idx_asv_retrieval ON artefact_section_versions (tenant_id, retrieval_set_id);
CREATE INDEX idx_asv_narrowed ON artefact_section_versions (tenant_id, created_at) WHERE scope_narrowed;

CREATE UNIQUE INDEX uq_run_idempotency ON generation_runs (tenant_id, principal_id, idempotency_key)
  WHERE idempotency_key IS NOT NULL;
CREATE INDEX idx_run_artefact ON generation_runs (tenant_id, artefact_id, created_at DESC);
CREATE INDEX idx_run_gate ON generation_runs (tenant_id, gate_expires_at) WHERE status = 'awaiting_outline_approval';

CREATE INDEX idx_rs_artefact ON retrieval_sets (tenant_id, artefact_id) WHERE artefact_id IS NOT NULL;
-- ART-9 / §21.1 delete a tender's sets by tender_id (an R-1 set has no artefact).
CREATE INDEX idx_rs_tender ON retrieval_sets (tenant_id, tender_id) WHERE tender_id IS NOT NULL;

CREATE INDEX idx_rsc_chunk ON retrieval_set_chunks (tenant_id, chunk_id);
CREATE INDEX idx_rsc_doc ON retrieval_set_chunks (tenant_id, collection_id, document_id, document_version);

CREATE UNIQUE INDEX uq_purge_event ON retrieval_set_purges (retrieval_set_id, event_id);
CREATE INDEX idx_purge_set ON retrieval_set_purges (tenant_id, retrieval_set_id);

CREATE UNIQUE INDEX uq_gr_section_version ON groundedness_reports (section_version_id);
CREATE UNIQUE INDEX uq_cc_section_version ON completeness_checks (section_version_id);
CREATE UNIQUE INDEX uq_fact_current ON document_facts (artefact_id, fact_key) WHERE superseded_at IS NULL;
CREATE INDEX idx_gc_sweep ON graph_checkpoints (tenant_id, created_at);
CREATE INDEX idx_qr_version ON query_refinements (tenant_id, refiner_version, created_at);
CREATE INDEX idx_ir_version ON instruction_refinements (tenant_id, refiner_version, created_at);

CREATE UNIQUE INDEX uq_audit_source_event ON artefact_audit_chain (tenant_id, source_event_id);
CREATE INDEX idx_audit_artefact ON artefact_audit_chain (tenant_id, artefact_id, seq);
CREATE INDEX idx_audit_created ON artefact_audit_chain (tenant_id, created_at);

-- ---------------------------------------------------------------------------------------
-- 4. Trigger functions (LLD 4.8.2) and the audit chain's canonical form
-- ---------------------------------------------------------------------------------------
CREATE FUNCTION reject_section_transition() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF NEW.state IS NOT DISTINCT FROM OLD.state THEN RETURN NEW; END IF;   -- a non-state update

  IF NOT (
       (OLD.state = 'pending'    AND NEW.state IN ('generating', 'gap'))
    OR (OLD.state = 'generating' AND NEW.state IN ('complete', 'gap', 'failed'))
    OR (OLD.state = 'complete'   AND NEW.state = 'generating')
    OR (OLD.state IN ('gap', 'failed') AND NEW.state = 'generating')
  ) THEN
    RAISE EXCEPTION 'artefact_sections: % to % is not a section transition', OLD.state, NEW.state
      USING ERRCODE = '23514';
  END IF;

  IF OLD.state = 'generating' AND NEW.state IN ('gap', 'failed') AND NEW.current_version_id IS NOT NULL THEN
    RAISE EXCEPTION 'artefact_sections: a section with a current version returns to complete, not %', NEW.state
      USING ERRCODE = '23514';
  END IF;

  IF OLD.state = 'gap'    THEN NEW.gap_reason := NULL; END IF;
  IF OLD.state = 'failed' THEN NEW.failure_reason := NULL; END IF;
  RETURN NEW;
END $$;

-- AC-2 canonical JSON: keys sorted by code point (C collation on UTF-8), no whitespace.
-- Python twin: rag.domain.generation.audit.canonical_json (json.dumps sort_keys, separators (',', ':'),
-- ensure_ascii False). Numbers inside metadata must be integers for the two to agree.
CREATE FUNCTION audit_canonical_json(j jsonb) RETURNS text LANGUAGE sql IMMUTABLE AS $$
  SELECT CASE jsonb_typeof(j)
    WHEN 'object' THEN '{' || coalesce((
      SELECT string_agg(to_jsonb(e.key)::text || ':' || audit_canonical_json(e.value), ',' ORDER BY e.key COLLATE "C")
      FROM jsonb_each(j) e), '') || '}'
    WHEN 'array' THEN '[' || coalesce((
      SELECT string_agg(audit_canonical_json(e.value), ',' ORDER BY e.ord)
      FROM jsonb_array_elements(j) WITH ORDINALITY e(value, ord)), '') || ']'
    ELSE j::text
  END
$$;

-- AC-2, AC-3, AC-4, AC-9: seq under a per-tenant advisory lock held to commit (a sequence would leave
-- gaps on rollback), prev/payload/entry hashes computed here so a client cannot supply them.
-- sha256(bytea) is built in (PG 11+); no pgcrypto call needed.
CREATE FUNCTION audit_chain_append() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
  last_seq  bigint;
  last_hash bytea;
  canon     text;
BEGIN
  PERFORM pg_advisory_xact_lock(hashtextextended('artefact_audit_chain:' || NEW.tenant_id::text, 0));
  SELECT c.seq, c.entry_hash INTO last_seq, last_hash
    FROM artefact_audit_chain c WHERE c.tenant_id = NEW.tenant_id ORDER BY c.seq DESC LIMIT 1;
  NEW.seq := coalesce(last_seq, 0) + 1;
  NEW.prev_hash := last_hash;                       -- NULL only at seq = 1
  canon := audit_canonical_json(jsonb_build_object(
    'tenant_id', NEW.tenant_id,
    'seq', NEW.seq,
    'occurred_at', to_char(NEW.occurred_at AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS.US"Z"'),
    'entry_type', NEW.entry_type,
    'action', NEW.action,
    'actor_type', NEW.actor_type,
    'actor_id', NEW.actor_id,
    'artefact_id', NEW.artefact_id,
    'target_type', NEW.target_type,
    'target_id', NEW.target_id,
    'source_service', NEW.source_service,
    'source_event_type', NEW.source_event_type,
    'source_event_id', NEW.source_event_id,
    'trace_id', NEW.trace_id,
    'metadata', NEW.metadata));
  NEW.payload_hash := sha256(convert_to(canon, 'UTF8'));
  NEW.entry_hash := sha256(coalesce(NEW.prev_hash, ''::bytea) || NEW.payload_hash);
  RETURN NEW;
END $$;

-- ---------------------------------------------------------------------------------------
-- 5. Triggers
-- ---------------------------------------------------------------------------------------
CREATE TRIGGER trg_version_immutable BEFORE UPDATE ON artefact_versions
  FOR EACH ROW EXECUTE FUNCTION reject_update_except('superseded_by_section_edit');
CREATE TRIGGER trg_audit_append_only BEFORE UPDATE OR DELETE ON artefact_audit_chain
  FOR EACH ROW EXECUTE FUNCTION reject_always();
CREATE TRIGGER trg_audit_chain_append BEFORE INSERT ON artefact_audit_chain
  FOR EACH ROW EXECUTE FUNCTION audit_chain_append();
CREATE TRIGGER trg_section_transition BEFORE UPDATE ON artefact_sections
  FOR EACH ROW EXECUTE FUNCTION reject_section_transition();

-- "likewise never updated after insert" (4.8.2). reject_always, not reject_update_except() with no
-- arguments: an empty TG_ARGV is NULL in plpgsql, which would make that function a no-op.
CREATE TRIGGER trg_rs_immutable BEFORE UPDATE ON retrieval_sets
  FOR EACH ROW EXECUTE FUNCTION reject_always();
CREATE TRIGGER trg_rsc_immutable BEFORE UPDATE ON retrieval_set_chunks
  FOR EACH ROW EXECUTE FUNCTION reject_always();
CREATE TRIGGER trg_rsp_immutable BEFORE UPDATE ON retrieval_set_purges
  FOR EACH ROW EXECUTE FUNCTION reject_always();
CREATE TRIGGER trg_asv_immutable BEFORE UPDATE ON artefact_section_versions
  FOR EACH ROW EXECUTE FUNCTION reject_always();
CREATE TRIGGER trg_avs_immutable BEFORE UPDATE ON artefact_version_sections
  FOR EACH ROW EXECUTE FUNCTION reject_always();
-- document_facts keeps only the supersession marker mutable.
CREATE TRIGGER trg_df_immutable BEFORE UPDATE ON document_facts
  FOR EACH ROW EXECUTE FUNCTION reject_update_except('superseded_at');

-- Database-managed columns on every mutable table.
CREATE TRIGGER trg_touch_updated_at BEFORE UPDATE ON artefacts
  FOR EACH ROW EXECUTE FUNCTION touch_updated_at();
CREATE TRIGGER trg_touch_updated_at BEFORE UPDATE ON artefact_sections
  FOR EACH ROW EXECUTE FUNCTION touch_updated_at();
CREATE TRIGGER trg_touch_updated_at BEFORE UPDATE ON generation_runs
  FOR EACH ROW EXECUTE FUNCTION touch_updated_at();
CREATE TRIGGER trg_touch_updated_at BEFORE UPDATE ON fact_conflicts
  FOR EACH ROW EXECUTE FUNCTION touch_updated_at();
CREATE TRIGGER trg_bump_record_version BEFORE UPDATE ON artefacts
  FOR EACH ROW EXECUTE FUNCTION bump_record_version();
CREATE TRIGGER trg_bump_record_version BEFORE UPDATE ON artefact_sections
  FOR EACH ROW EXECUTE FUNCTION bump_record_version();
CREATE TRIGGER trg_bump_record_version BEFORE UPDATE ON generation_runs
  FOR EACH ROW EXECUTE FUNCTION bump_record_version();
CREATE TRIGGER trg_bump_record_version BEFORE UPDATE ON fact_conflicts
  FOR EACH ROW EXECUTE FUNCTION bump_record_version();

-- ---------------------------------------------------------------------------------------
-- 6. RLS (4.8.1): ENABLE + FORCE + uniform policy on every table (processed_events is the library's)
-- ---------------------------------------------------------------------------------------
DO $$
DECLARE t text;
BEGIN
  FOREACH t IN ARRAY ARRAY[
    'artefacts', 'artefact_versions', 'artefact_version_sections', 'artefact_sections',
    'artefact_section_versions', 'generation_runs', 'retrieval_sets', 'retrieval_set_chunks',
    'retrieval_set_purges', 'groundedness_reports', 'claim_verifications', 'completeness_checks',
    'document_facts', 'fact_conflicts', 'graph_checkpoints', 'query_refinements',
    'instruction_refinements', 'artefact_audit_chain']
  LOOP
    EXECUTE format('ALTER TABLE %I ENABLE ROW LEVEL SECURITY', t);
    EXECUTE format('ALTER TABLE %I FORCE ROW LEVEL SECURITY', t);
    EXECUTE format($p$CREATE POLICY tenant_isolation ON %I
      USING      (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid)
      WITH CHECK (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid)$p$, t);
  END LOOP;
END $$;

-- ---------------------------------------------------------------------------------------
-- 7. Grants (4.1). rag_app's SELECT/INSERT/UPDATE baseline is the default privilege from 0000.
-- ---------------------------------------------------------------------------------------
REVOKE ALL ON
  artefacts, artefact_versions, artefact_version_sections, artefact_sections,
  artefact_section_versions, generation_runs, retrieval_sets, retrieval_set_chunks,
  retrieval_set_purges, groundedness_reports, claim_verifications, completeness_checks,
  document_facts, fact_conflicts, graph_checkpoints, query_refinements,
  instruction_refinements, artefact_audit_chain
FROM PUBLIC;

REVOKE INSERT, UPDATE ON graph_checkpoints FROM rag_app;
GRANT SELECT, INSERT, UPDATE, DELETE ON graph_checkpoints TO rag_activity;
-- DELETE is used only by the TenderPurged / tenant-offboarding deletes (ART-9, EVT-15, 21.1).
GRANT DELETE ON
  artefacts, artefact_versions, artefact_version_sections, artefact_sections,
  artefact_section_versions, generation_runs, retrieval_sets, retrieval_set_chunks,
  retrieval_set_purges, groundedness_reports, claim_verifications, completeness_checks,
  document_facts, fact_conflicts, query_refinements, instruction_refinements,
  graph_checkpoints
TO rag_app;
-- artefact_audit_chain is granted no DELETE to anyone (AC-1).
GRANT SELECT, INSERT, UPDATE ON
  artefacts, artefact_versions, artefact_version_sections, artefact_sections,
  artefact_section_versions, generation_runs, retrieval_sets, retrieval_set_chunks,
  groundedness_reports, claim_verifications, completeness_checks, document_facts,
  fact_conflicts, query_refinements, instruction_refinements, artefact_audit_chain
TO rag_activity;

RESET ROLE;
