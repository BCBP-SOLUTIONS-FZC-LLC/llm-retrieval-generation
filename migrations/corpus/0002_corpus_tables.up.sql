-- LLD 4.7 llm_corpus tables, 4.8 RLS and triggers, 4.1 per-table grants. Owned by rag_owner.
SET ROLE rag_owner;

-- 4.7.1 collections ---------------------------------------------------------------------------
CREATE TABLE collections (
  id                      uuid PRIMARY KEY,     -- UUIDv5, derived, never random (COL-11)
  tenant_id               uuid NOT NULL,
  type                    collection_type NOT NULL,
  tender_id               uuid NOT NULL,        -- real for 'tender', sentinel otherwise
  restricted              boolean NOT NULL,     -- immutable after insert, COL-2
  physical_collection     text NOT NULL,
  shard_key               text NOT NULL,
  active_index_generation int NOT NULL DEFAULT 1,
  purge_epoch             bigint NOT NULL DEFAULT 0,
  status                  collection_status NOT NULL DEFAULT 'provisioning',
  status_before_freeze    collection_status,    -- the status a freeze interrupted, COL-12
  weight_override         numeric(3,2),
  owning_departments      uuid[] NOT NULL DEFAULT '{}',  -- ordinary tenders only (COL-6)
  promoted_from           uuid,                          -- set on a promotion target, COL-7
  restricted_announced_at timestamptz,                   -- set by the TenderRestricted consumer, COL-8
  tender_closed_at        timestamptz,                   -- set once on a terminal outcome, COL-14
  -- Deviation (docs/implementation-plan.md): the LLD (5.7.16) defers this column to Phase 2, but C-I8
  -- cannot answer 409 MIGRATION_MISMATCH without it. NULL except on a promotion target.
  migration_id            uuid,
  record_version          bigint NOT NULL DEFAULT 1 CHECK (record_version > 0),
  created_at              timestamptz NOT NULL DEFAULT now(),
  updated_at              timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT ck_coll_weight
    CHECK (weight_override IS NULL OR weight_override BETWEEN 0.00 AND 1.00),
  CONSTRAINT ck_coll_promotion CHECK (promoted_from IS NULL OR restricted),
  CONSTRAINT ck_coll_freeze CHECK (
    (status = 'frozen') = (status_before_freeze IS NOT NULL)),
  CONSTRAINT fk_coll_promoted_from FOREIGN KEY (promoted_from) REFERENCES collections(id)
);

-- 4.7.2 collection_grants ---------------------------------------------------------------------
CREATE TABLE collection_grants (
  id            uuid PRIMARY KEY DEFAULT uuidv7(),
  collection_id uuid NOT NULL REFERENCES collections(id),
  tenant_id     uuid NOT NULL,
  subject_type  grant_subject_type NOT NULL,
  subject_id    uuid NOT NULL,
  level         grant_level NOT NULL,
  granted_by    uuid NOT NULL,
  granted_at    timestamptz NOT NULL DEFAULT now(),
  projected_at  timestamptz NOT NULL DEFAULT now(),
  source        text NOT NULL DEFAULT 'iam_overlay',
  expires_at    timestamptz,
  deleted_at    timestamptz,
  record_version bigint NOT NULL DEFAULT 1 CHECK (record_version > 0),
  created_at    timestamptz NOT NULL DEFAULT now(),
  updated_at    timestamptz NOT NULL DEFAULT now()
);

-- 4.7.3 index_generations ---------------------------------------------------------------------
CREATE TABLE index_generations (
  collection_id  uuid NOT NULL REFERENCES collections(id),
  generation     int NOT NULL,
  tenant_id      uuid NOT NULL,
  embedding_profile text NOT NULL,
  chunker_version   text NOT NULL,
  status         index_generation_status NOT NULL,
  registered_at  timestamptz NOT NULL DEFAULT now(),
  verified_at    timestamptz,
  failed_checks  jsonb,
  activated_at   timestamptz,
  retired_at     timestamptz,
  record_version bigint NOT NULL DEFAULT 1 CHECK (record_version > 0),
  created_at     timestamptz NOT NULL DEFAULT now(),
  updated_at     timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (collection_id, generation),
  CONSTRAINT ck_gen_verified CHECK (status NOT IN ('verified','active','retired') OR verified_at IS NOT NULL),
  CONSTRAINT ck_gen_failed   CHECK (status <> 'failed' OR failed_checks IS NOT NULL)
);

-- 4.7.4 retention_policies --------------------------------------------------------------------
CREATE TABLE retention_policies (
  collection_id  uuid PRIMARY KEY REFERENCES collections(id),
  tenant_id      uuid NOT NULL,
  retain_days    int NOT NULL CHECK (retain_days > 0),
  legal_hold     boolean NOT NULL DEFAULT false,
  record_version bigint NOT NULL DEFAULT 1 CHECK (record_version > 0),
  updated_by     uuid NOT NULL,
  created_at     timestamptz NOT NULL DEFAULT now(),
  updated_at     timestamptz NOT NULL DEFAULT now()
);

-- 4.7.5 tenant_lifecycle ----------------------------------------------------------------------
CREATE TABLE tenant_lifecycle (
  tenant_id   uuid PRIMARY KEY,
  state       text NOT NULL CHECK (state IN ('frozen','purged')),
  cause       text,
  changed_at  timestamptz NOT NULL DEFAULT now(),
  record_version bigint NOT NULL DEFAULT 1 CHECK (record_version > 0),
  created_at  timestamptz NOT NULL DEFAULT now(),
  updated_at  timestamptz NOT NULL DEFAULT now()
);

-- Indexes -------------------------------------------------------------------------------------
-- A promotion holds two rows for one tender until cutover (restricted differs); a second target
-- for the same source fails here, which makes promotion idempotent (LD-33).
CREATE UNIQUE INDEX uq_coll_tender ON collections (tenant_id, tender_id, type, restricted)
  WHERE status <> 'superseded';
-- Exactly one row serves at any instant. A cutover supersedes the old row before it activates the new one.
CREATE UNIQUE INDEX uq_coll_active ON collections (tenant_id, tender_id, type)
  WHERE status = 'active';
CREATE INDEX idx_coll_active ON collections (tenant_id, status) WHERE status = 'active';
CREATE UNIQUE INDEX uq_grant_active ON collection_grants (collection_id, subject_type, subject_id)
  WHERE deleted_at IS NULL;
CREATE INDEX idx_grant_subject ON collection_grants (tenant_id, subject_type, subject_id)
  WHERE deleted_at IS NULL;
CREATE UNIQUE INDEX uq_gen_active ON index_generations (collection_id) WHERE status = 'active';

-- Trigger functions that belong to this slice (4.7.5, 4.8.2) -----------------------------------
CREATE FUNCTION reject_purged_change() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF OLD.state = 'purged' THEN
    RAISE EXCEPTION 'tenant_lifecycle % is purged and terminal', OLD.tenant_id USING ERRCODE = '23514';
  END IF;
  IF TG_OP = 'DELETE' THEN RETURN OLD; END IF;
  RETURN NEW;
END $$;

-- IG-3: this service writes every transition, the build states on Ingestion's request (LD-66).
-- IG-5: a generation reaches 'active' only from 'verified', for any caller.
CREATE FUNCTION reject_generation_transition() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
  -- Membership, not name: the service connects as a login role granted these NOLOGIN group
  -- roles and pgcommon never runs SET ROLE, so current_user is the login role.
  by_us      boolean := pg_has_role(current_user, 'rag_app', 'member')
                        OR pg_has_role(current_user, 'rag_activity', 'member');
BEGIN
  IF TG_OP = 'INSERT' THEN
    IF NEW.status IN ('registered', 'verified', 'active') AND by_us THEN RETURN NEW; END IF;
    RAISE EXCEPTION 'generation cannot be created as % by %', NEW.status, current_user
      USING ERRCODE = '23514';
  END IF;

  IF OLD.status = NEW.status THEN RETURN NEW; END IF;   -- a non-status update

  CASE
    WHEN OLD.status = 'registered' AND NEW.status = 'building'  AND by_us     THEN NULL;
    WHEN OLD.status = 'building'   AND NEW.status = 'verified'  AND by_us     THEN NULL;
    WHEN OLD.status = 'building'   AND NEW.status = 'failed'    AND by_us     THEN NULL;
    WHEN OLD.status = 'verified'   AND NEW.status = 'active'    AND by_us     THEN NULL;
    WHEN OLD.status = 'active'     AND NEW.status = 'retired'   AND by_us     THEN NULL;
    ELSE
      RAISE EXCEPTION 'generation transition % -> % refused for %',
        OLD.status, NEW.status, current_user USING ERRCODE = '23514';
  END CASE;
  RETURN NEW;
END $$;

-- Triggers ------------------------------------------------------------------------------------
-- Placement is fixed at provisioning (COL-2).
CREATE TRIGGER trg_collection_restricted_immutable BEFORE UPDATE ON collections
  FOR EACH ROW EXECUTE FUNCTION reject_column_change('restricted');
CREATE TRIGGER trg_generation_transition BEFORE INSERT OR UPDATE ON index_generations
  FOR EACH ROW EXECUTE FUNCTION reject_generation_transition();
CREATE TRIGGER trg_tl_purged_terminal BEFORE UPDATE OR DELETE ON tenant_lifecycle
  FOR EACH ROW EXECUTE FUNCTION reject_purged_change();

CREATE TRIGGER trg_touch_updated_at BEFORE UPDATE ON collections
  FOR EACH ROW EXECUTE FUNCTION touch_updated_at();
CREATE TRIGGER trg_bump_record_version BEFORE UPDATE ON collections
  FOR EACH ROW EXECUTE FUNCTION bump_record_version();
CREATE TRIGGER trg_touch_updated_at BEFORE UPDATE ON collection_grants
  FOR EACH ROW EXECUTE FUNCTION touch_updated_at();
CREATE TRIGGER trg_bump_record_version BEFORE UPDATE ON collection_grants
  FOR EACH ROW EXECUTE FUNCTION bump_record_version();
CREATE TRIGGER trg_touch_updated_at BEFORE UPDATE ON index_generations
  FOR EACH ROW EXECUTE FUNCTION touch_updated_at();
CREATE TRIGGER trg_bump_record_version BEFORE UPDATE ON index_generations
  FOR EACH ROW EXECUTE FUNCTION bump_record_version();
CREATE TRIGGER trg_touch_updated_at BEFORE UPDATE ON retention_policies
  FOR EACH ROW EXECUTE FUNCTION touch_updated_at();
CREATE TRIGGER trg_bump_record_version BEFORE UPDATE ON retention_policies
  FOR EACH ROW EXECUTE FUNCTION bump_record_version();
CREATE TRIGGER trg_touch_updated_at BEFORE UPDATE ON tenant_lifecycle
  FOR EACH ROW EXECUTE FUNCTION touch_updated_at();
CREATE TRIGGER trg_bump_record_version BEFORE UPDATE ON tenant_lifecycle
  FOR EACH ROW EXECUTE FUNCTION bump_record_version();

-- RLS (4.8.1): FORCE on every table; revoke from PUBLIC --------------------------------------
ALTER TABLE collections        ENABLE ROW LEVEL SECURITY;
ALTER TABLE collections        FORCE ROW LEVEL SECURITY;
ALTER TABLE collection_grants  ENABLE ROW LEVEL SECURITY;
ALTER TABLE collection_grants  FORCE ROW LEVEL SECURITY;
ALTER TABLE index_generations  ENABLE ROW LEVEL SECURITY;
ALTER TABLE index_generations  FORCE ROW LEVEL SECURITY;
ALTER TABLE retention_policies ENABLE ROW LEVEL SECURITY;
ALTER TABLE retention_policies FORCE ROW LEVEL SECURITY;
ALTER TABLE tenant_lifecycle   ENABLE ROW LEVEL SECURITY;
ALTER TABLE tenant_lifecycle   FORCE ROW LEVEL SECURITY;
REVOKE ALL ON collections, collection_grants, index_generations, retention_policies, tenant_lifecycle
  FROM PUBLIC;

CREATE POLICY tenant_isolation ON index_generations
  USING      (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid)
  WITH CHECK (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid);
CREATE POLICY tenant_isolation ON retention_policies
  USING      (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid)
  WITH CHECK (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid);
CREATE POLICY tenant_isolation ON tenant_lifecycle
  USING      (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid)
  WITH CHECK (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid);

-- The read side admits the PLATFORM sentinel, the write side must not. Two policies, so that
-- editing one half cannot widen the other.
CREATE POLICY tenant_read ON collections FOR SELECT
  USING (
    tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid
    OR tenant_id = '00000000-0000-7000-8000-4c4c4d000003'::uuid
  );
CREATE POLICY tenant_write ON collections FOR ALL
  USING      (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid)
  WITH CHECK (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid);
CREATE POLICY tenant_read ON collection_grants FOR SELECT
  USING (
    tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid
    OR tenant_id = '00000000-0000-7000-8000-4c4c4d000003'::uuid
  );
CREATE POLICY tenant_write ON collection_grants FOR ALL
  USING      (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid)
  WITH CHECK (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid);

-- Grants (4.1). SELECT, INSERT, UPDATE for rag_app come from 0000's default privileges. -----------
GRANT SELECT, INSERT, UPDATE ON collections, collection_grants, index_generations,
  retention_policies TO rag_app;
GRANT SELECT, INSERT, UPDATE, DELETE ON tenant_lifecycle TO rag_app;
-- DELETE below is used only by the second phase of a tenant purge (EVT-15, 21.1)
GRANT DELETE ON collections, collection_grants, index_generations, retention_policies TO rag_app;
GRANT SELECT ON collections, index_generations TO rag_activity;
-- No grant to Ingestion (rev 2.83, LD-66): this service writes every index_generations row.

RESET ROLE;
