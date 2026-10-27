"""``0002_corpus_tables``: syntax (pglast) and the LLD rules visible in the text of the DDL."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

DIR = Path(__file__).resolve().parents[2] / "migrations" / "corpus"
UP = (DIR / "0002_corpus_tables.up.sql").read_text()
DOWN = (DIR / "0002_corpus_tables.down.sql").read_text()
TABLES = [
    "collections",
    "collection_grants",
    "index_generations",
    "retention_policies",
    "tenant_lifecycle",
]


def test_both_files_parse_as_postgres_sql() -> None:
    """Syntax check with the Postgres parser (skipped where pglast is absent)."""
    pglast = pytest.importorskip("pglast")
    assert len(pglast.parse_sql(UP)) > 40
    assert pglast.parse_sql(DOWN)


def test_owned_by_rag_owner_and_reset() -> None:
    """README convention: SET ROLE rag_owner ... RESET ROLE, both ways."""
    for sql in (UP, DOWN):
        assert sql.lstrip().startswith(("--", "SET ROLE rag_owner;"))
        assert "SET ROLE rag_owner;" in sql and sql.rstrip().endswith("RESET ROLE;")


def test_five_tables_and_the_down_drops_them_all() -> None:
    """LLD §4.7: five tables; the down file reverses every object the up creates."""
    assert re.findall(r"CREATE TABLE (\w+)", UP) == [
        "collections",
        "collection_grants",
        "index_generations",
        "retention_policies",
        "tenant_lifecycle",
    ]
    for t in TABLES:
        assert f"DROP TABLE IF EXISTS {t};" in DOWN
    for fn in re.findall(r"CREATE FUNCTION (\w+)", UP):
        assert f"DROP FUNCTION IF EXISTS {fn}();" in DOWN


@pytest.mark.parametrize("table", TABLES)
def test_rls_enabled_forced_and_revoked_from_public(table: str) -> None:
    """RLS-1 / §4.8.1: ENABLE + FORCE on every table, nothing for PUBLIC."""
    assert f"ALTER TABLE {table}" in UP
    assert re.search(rf"ALTER TABLE {table}\s+ENABLE ROW LEVEL SECURITY;", UP)
    assert re.search(rf"ALTER TABLE {table}\s+FORCE ROW LEVEL SECURITY;", UP)
    assert re.search(rf"REVOKE ALL ON [^;]*\b{table}\b[^;]* FROM PUBLIC;", UP)


def test_policies_uniform_and_the_read_write_pair() -> None:
    """Uniform ``tenant_isolation`` on three tables; the pair on collections and grants."""
    uniform = re.findall(r"CREATE POLICY tenant_isolation ON (\w+)", UP)
    assert sorted(uniform) == ["index_generations", "retention_policies", "tenant_lifecycle"]
    for t in ("collections", "collection_grants"):
        assert f"CREATE POLICY tenant_read ON {t} FOR SELECT" in UP
        assert f"CREATE POLICY tenant_write ON {t} FOR ALL" in UP
        assert f"CREATE POLICY tenant_isolation ON {t}" not in UP
    reads = re.findall(r"CREATE POLICY tenant_read.*?;", UP, re.S)
    writes = re.findall(r"CREATE POLICY tenant_write.*?;", UP, re.S)
    assert all("00000000-0000-7000-8000-4c4c4d000003" in r for r in reads)  # PLATFORM on read only
    assert not any("4c4c4d000003" in w for w in writes)
    assert UP.count("NULLIF(current_setting('app.tenant_id', true), '')::uuid") >= 10


def test_every_mutable_table_has_both_managed_column_triggers() -> None:
    """§4.8.2: touch_updated_at and bump_record_version on all five."""
    for t in TABLES:
        for trg, fn in (
            ("trg_touch_updated_at", "touch_updated_at"),
            ("trg_bump_record_version", "bump_record_version"),
        ):
            assert re.search(
                rf"CREATE TRIGGER {trg} BEFORE UPDATE ON {t}\s+FOR EACH ROW "
                rf"EXECUTE FUNCTION {fn}\(\);",
                UP,
            )


def test_lld_triggers_present() -> None:
    """COL-2, IG-5, TL-1."""
    assert "trg_collection_restricted_immutable BEFORE UPDATE ON collections" in UP
    assert "reject_column_change('restricted')" in UP
    assert "trg_generation_transition BEFORE INSERT OR UPDATE ON index_generations" in UP
    assert "trg_tl_purged_terminal BEFORE UPDATE OR DELETE ON tenant_lifecycle" in UP


def test_constraints_and_indexes_from_the_lld() -> None:
    """ck_coll_*, uq_coll_*, uq_gen_active, uq_grant_active."""
    for name in (
        "ck_coll_weight",
        "ck_coll_promotion",
        "ck_coll_freeze",
        "fk_coll_promoted_from",
        "ck_gen_verified",
        "ck_gen_failed",
    ):
        assert name in UP
    assert (
        "CREATE UNIQUE INDEX uq_coll_tender ON collections (tenant_id, tender_id, type, restricted)"
        in UP
    )
    assert "CREATE UNIQUE INDEX uq_coll_active ON collections (tenant_id, tender_id, type)" in UP
    assert (
        "CREATE UNIQUE INDEX uq_gen_active ON index_generations (collection_id) "
        "WHERE status = 'active';" in UP
    )
    assert "CREATE UNIQUE INDEX uq_grant_active" in UP


def test_grants_follow_section_4_1() -> None:
    """rag_app DELETE lists, tenant_lifecycle DML, rag_activity read-only, none for Ingestion."""
    assert "GRANT SELECT, INSERT, UPDATE, DELETE ON tenant_lifecycle TO rag_app;" in UP
    assert re.search(
        r"GRANT DELETE ON collections, collection_grants, index_generations, "
        r"retention_policies TO rag_app;",
        UP,
    )
    assert "GRANT SELECT ON collections, index_generations TO rag_activity;" in UP
    assert "TO ingestion" not in UP.lower()  # LD-66: Ingestion holds no grant


def test_uuidv7_default_on_grants_and_derived_key_on_collections() -> None:
    """LD-54 and COL-11: collections.id has no default; the grant PK defaults to uuidv7()."""
    assert "id            uuid PRIMARY KEY DEFAULT uuidv7()," in UP
    assert re.search(r"CREATE TABLE collections \(\s+id\s+uuid PRIMARY KEY,", UP)
