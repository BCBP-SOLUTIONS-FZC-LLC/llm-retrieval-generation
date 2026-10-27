"""Schema behaviour of ``0002_generation_tables`` on real Postgres: RLS, grants and triggers."""

# ruff: noqa: E501 (SQL literals)

from __future__ import annotations

import asyncio
from uuid import UUID, uuid4

import asyncpg
import pytest
from tests.repository import seed
from tests.repository.support import Db, as_tenant

from rag.domain.generation import audit
from rag.domain.shared.enums import AuditActorType
from rag.infrastructure.persistence.generation.repositories import PgAuditChainRepository

pytestmark = pytest.mark.integration

TENANT_A = UUID("0a3f0000-0000-7000-8000-00000000000a")
TENANT_B = UUID("0a3f0000-0000-7000-8000-00000000000b")
TENDER = UUID("0a3f0000-0000-7000-8000-0000000000a1")

RLS_TABLES = [
    "artefacts", "artefact_versions", "artefact_version_sections", "artefact_sections",
    "artefact_section_versions", "generation_runs", "retrieval_sets", "retrieval_set_chunks",
    "retrieval_set_purges", "groundedness_reports", "claim_verifications", "completeness_checks",
    "document_facts", "fact_conflicts", "graph_checkpoints", "query_refinements",
    "instruction_refinements", "artefact_audit_chain",
]  # fmt: skip


async def seed_tenant(db: Db, tenant: UUID) -> seed.Graph:
    async def work(c: asyncpg.Connection) -> seed.Graph:
        g = await seed.graph(c, tenant, TENDER)
        await seed.facts_and_refinements(c, g)
        return g

    with as_tenant(tenant):
        g = await db.app_tx.run_in_tx(work)
        await db.activity_tx.run_in_tx(lambda c: seed.checkpoint(c, g))
        await db.app_tx.run_in_tx(lambda c: _audit(c, tenant, g.artefact, "seed"))
    return g


async def _audit(c: asyncpg.Connection, tenant: UUID, artefact: UUID, event: str) -> None:
    await PgAuditChainRepository().append(c, _draft(tenant, artefact, event))


def _draft(tenant: UUID, artefact: UUID, event: str, **kw: object) -> audit.AuditEntryDraft:
    from datetime import UTC, datetime

    base: dict[str, object] = dict(
        tenant_id=tenant,
        occurred_at=datetime(2026, 4, 2, 9, 14, 0, 123456, tzinfo=UTC),
        entry_type="llm.generation.completed",
        action="create",
        actor_type=AuditActorType.USER,
        actor_id=seed.USER,
        artefact_id=artefact,
        target_type="artefact_version",
        target_id=str(uuid4()),
        source_event_type="llm.generation.completed",
        source_event_id=event,
        metadata={"prompt_hash": "sha256:aa", "section_count": 2, "note": "naïve – ünï"},
    )
    return audit.AuditEntryDraft(**{**base, **kw})  # type: ignore[arg-type]


# --- RLS ----------------------------------------------------------------------------------


async def test_every_table_but_processed_events_has_enabled_and_forced_rls(db: Db) -> None:
    """RLS-1: ENABLE + FORCE, and the single named exemption."""
    conn = await db.admin()
    try:
        flags = {
            r["relname"]: (r["relrowsecurity"], r["relforcerowsecurity"])
            for r in await conn.fetch(
                "SELECT relname, relrowsecurity, relforcerowsecurity FROM pg_class c "
                "JOIN pg_namespace n ON n.oid = c.relnamespace "
                "WHERE n.nspname = 'public' AND c.relkind = 'r'"
            )
        }
        policies = {r[0] for r in await conn.fetch("SELECT tablename FROM pg_policies")}
        owners = {
            r[0]
            for r in await conn.fetch(
                "SELECT DISTINCT tableowner FROM pg_tables WHERE schemaname='public' AND tablename = ANY($1)",
                RLS_TABLES,
            )
        }
    finally:
        await conn.close()
    for table in RLS_TABLES:
        assert flags[table] == (True, True), table
        assert table in policies, table
    assert flags["processed_events"] == (False, False)
    assert owners == {"rag_owner"}


async def test_cross_tenant_reads_return_zero_rows_in_every_table(db: Db) -> None:
    """RLS-3: tenant B sees none of tenant A's rows, and no GUC sees nothing at all."""
    await seed_tenant(db, TENANT_A)
    await seed_tenant(db, TENANT_B)

    async def counts(c: asyncpg.Connection, tenant: UUID) -> tuple[int, int]:
        mine = other = 0
        for table in RLS_TABLES:
            mine += await c.fetchval(f"SELECT count(*) FROM {table} WHERE tenant_id = $1", tenant)
            other += await c.fetchval(f"SELECT count(*) FROM {table} WHERE tenant_id <> $1", tenant)
        return mine, other

    for tenant in (TENANT_A, TENANT_B):
        with as_tenant(tenant):
            mine, other = await db.app_tx.run_in_tx(lambda c, t=tenant: counts(c, t))  # type: ignore[misc]
        assert mine > 0 and other == 0
    with as_tenant(None):
        total = await db.app_tx.run_in_tx(
            lambda c: c.fetchval(
                "SELECT (SELECT count(*) FROM artefacts) + (SELECT count(*) FROM artefact_audit_chain)"
            )
        )
    assert total == 0


async def test_cross_tenant_writes_are_rejected_and_deletes_match_nothing(db: Db) -> None:
    """WITH CHECK refuses a foreign tenant_id; UPDATE/DELETE of a foreign row touch zero rows."""
    g = await seed_tenant(db, TENANT_A)
    with as_tenant(TENANT_B):
        with pytest.raises(asyncpg.InsufficientPrivilegeError):
            await db.app_tx.run_in_tx(
                lambda c: c.execute(
                    "INSERT INTO artefacts (tenant_id, tender_id, kind, capability, created_by) "
                    "VALUES ($1, $2, 'document', 'x', $3)",
                    TENANT_A,
                    TENDER,
                    seed.USER,
                )
            )
        deleted = await db.app_tx.run_in_tx(
            lambda c: c.execute(
                "DELETE FROM retrieval_set_chunks WHERE retrieval_set_id = $1", g.retrieval_set
            )
        )
        updated = await db.app_tx.run_in_tx(
            lambda c: c.execute(
                "UPDATE artefacts SET capability = 'hijack' WHERE id = $1", g.artefact
            )
        )
    assert (deleted, updated) == ("DELETE 0", "UPDATE 0")
    with as_tenant(TENANT_A):
        cap = await db.app_tx.run_in_tx(
            lambda c: c.fetchval("SELECT capability FROM artefacts WHERE id = $1", g.artefact)
        )
    assert cap == "technical_proposal"


# --- grants -------------------------------------------------------------------------------


async def test_grants_follow_the_llm_4_1_split(db: Db) -> None:
    """rag_app: no write on graph_checkpoints; activity: nothing on processed_events; no DELETE on the chain."""
    g = await seed_tenant(db, TENANT_A)
    with as_tenant(TENANT_A):
        with pytest.raises(asyncpg.InsufficientPrivilegeError):  # rag_app cannot write checkpoints
            await db.app_tx.run_in_tx(lambda c: seed.checkpoint(c, g))
        with pytest.raises(asyncpg.InsufficientPrivilegeError):  # nor delete from the chain
            await db.app_tx.run_in_tx(lambda c: c.execute("DELETE FROM artefact_audit_chain"))
        with pytest.raises(asyncpg.InsufficientPrivilegeError):  # activity: no processed_events
            await db.activity_tx.run_in_tx(
                lambda c: c.fetchval("SELECT count(*) FROM processed_events")
            )
        with pytest.raises(
            asyncpg.InsufficientPrivilegeError
        ):  # activity: no DELETE anywhere on artefacts
            await db.activity_tx.run_in_tx(lambda c: c.execute("DELETE FROM artefacts"))
        await db.activity_tx.run_in_tx(lambda c: seed.checkpoint(c, g))  # but may write checkpoints
        await db.app_tx.run_in_tx(
            lambda c: c.execute(
                "INSERT INTO processed_events (event_id, consumer) VALUES ($1, 'c')", uuid4()
            )
        )


# --- triggers: immutability ---------------------------------------------------------------


async def test_trg_version_immutable_allows_only_the_supersession_marker(db: Db) -> None:
    """AV-1 / AV-6."""
    g = await seed_tenant(db, TENANT_A)
    with as_tenant(TENANT_A):
        with pytest.raises(asyncpg.CheckViolationError, match="prompt_hash is immutable"):
            await db.app_tx.run_in_tx(
                lambda c: c.execute(
                    "UPDATE artefact_versions SET prompt_hash = 'x' WHERE id = $1", g.version
                )
            )
        status = await db.app_tx.run_in_tx(
            lambda c: c.execute(
                "UPDATE artefact_versions SET superseded_by_section_edit = true WHERE id = $1",
                g.version,
            )
        )
    assert status == "UPDATE 1"


@pytest.mark.parametrize(
    ("table", "set_clause", "key"),
    [
        ("retrieval_sets", "verdict_reason = 'x'", "id = '{rs}'"),
        ("retrieval_set_chunks", "excerpt = 'x'", "retrieval_set_id = '{rs}' AND rank = 1"),
        ("artefact_section_versions", "serving_tier = 'x'", "id = '{sv}'"),
        ("artefact_version_sections", "tenant_id = tenant_id", "artefact_version_id = '{ver}'"),
    ],
)
async def test_append_only_tables_refuse_every_update(
    db: Db, table: str, set_clause: str, key: str
) -> None:
    """RS-1, ASV-1, AVS-1: never updated after insert (they leave only through the ART-9 delete)."""
    g = await seed_tenant(db, TENANT_A)
    where = key.format(rs=g.retrieval_set, sv=g.section_version, ver=g.version)
    with as_tenant(TENANT_A):
        with pytest.raises(asyncpg.CheckViolationError, match="append-only"):
            await db.app_tx.run_in_tx(
                lambda c: c.execute(f"UPDATE {table} SET {set_clause} WHERE {where}")
            )


async def test_document_facts_allow_only_the_supersession_column(db: Db) -> None:
    """DF-1."""
    g = await seed_tenant(db, TENANT_A)
    with as_tenant(TENANT_A):
        with pytest.raises(asyncpg.CheckViolationError):
            await db.app_tx.run_in_tx(
                lambda c: c.execute("UPDATE document_facts SET value = 'ladder'")
            )
        status = await db.app_tx.run_in_tx(
            lambda c: c.execute("UPDATE document_facts SET superseded_at = now()")
        )
        # DF-2: a second current row for the same key is allowed only once the first is superseded
        await db.app_tx.run_in_tx(
            lambda c: c.execute(
                "INSERT INTO document_facts (artefact_id, tenant_id, fact_key, value, established_by_section_id) "
                "VALUES ($1, $2, 'access_method', 'ladder', $3)",
                g.artefact,
                g.tenant,
                g.section,
            )
        )
        with pytest.raises(asyncpg.UniqueViolationError):
            await db.app_tx.run_in_tx(
                lambda c: c.execute(
                    "INSERT INTO document_facts (artefact_id, tenant_id, fact_key, value, established_by_section_id) "
                    "VALUES ($1, $2, 'access_method', 'rope', $3)",
                    g.artefact,
                    g.tenant,
                    g.section,
                )
            )
    assert status == "UPDATE 1"


# --- triggers: managed columns and section machine ----------------------------------------


async def test_record_version_and_updated_at_are_database_managed(db: Db) -> None:
    """A change bumps record_version by one and touches updated_at; a no-op change does neither."""
    g = await seed_tenant(db, TENANT_A)

    async def read(c: asyncpg.Connection) -> tuple[int, object]:
        r = await c.fetchrow(
            "SELECT record_version, updated_at FROM artefacts WHERE id = $1", g.artefact
        )
        return r["record_version"], r["updated_at"]

    with as_tenant(TENANT_A):
        v0, t0 = await db.app_tx.run_in_tx(read)
        await db.app_tx.run_in_tx(
            lambda c: c.execute(
                "UPDATE artefacts SET capability = capability WHERE id = $1", g.artefact
            )
        )
        assert await db.app_tx.run_in_tx(read) == (v0, t0)
        # a client-supplied record_version is overwritten by the trigger
        await db.app_tx.run_in_tx(
            lambda c: c.execute(
                "UPDATE artefacts SET capability = 'x', record_version = 99 WHERE id = $1",
                g.artefact,
            )
        )
        v1, t1 = await db.app_tx.run_in_tx(read)
    assert v1 == v0 + 1 and t1 > t0  # type: ignore[operator]


async def test_section_transitions_enforced_by_trigger(db: Db) -> None:
    """AS-5, AS-7: unlisted pairs refused; reasons cleared on exit; no gap/failed with a version."""
    g = await seed_tenant(db, TENANT_A)

    async def add_section(c: asyncpg.Connection, ordinal: int) -> UUID:
        return await c.fetchval(  # type: ignore[no-any-return]
            "INSERT INTO artefact_sections (artefact_id, tenant_id, section_type, ordinal) "
            "VALUES ($1, $2, 'scope', $3) RETURNING id",
            g.artefact,
            g.tenant,
            ordinal,
        )

    async def state(c: asyncpg.Connection, sid: UUID) -> tuple[str, str | None, str | None]:
        r = await c.fetchrow(
            "SELECT state, gap_reason, failure_reason FROM artefact_sections WHERE id = $1", sid
        )
        return r["state"], r["gap_reason"], r["failure_reason"]

    with as_tenant(TENANT_A):
        sid = await db.app_tx.run_in_tx(lambda c: add_section(c, 2))
        # pending -> complete is not an edge
        with pytest.raises(asyncpg.CheckViolationError, match="not a section transition"):
            await db.app_tx.run_in_tx(
                lambda c: c.execute(
                    "UPDATE artefact_sections SET state = 'complete' WHERE id = $1", sid
                )
            )
        # pending -> gap -> generating clears gap_reason
        await db.app_tx.run_in_tx(
            lambda c: c.execute(
                "UPDATE artefact_sections SET state = 'gap', gap_reason = 'required_input_missing' WHERE id = $1",
                sid,
            )
        )
        assert await db.app_tx.run_in_tx(lambda c: state(c, sid)) == (
            "gap",
            "required_input_missing",
            None,
        )
        await db.app_tx.run_in_tx(
            lambda c: c.execute(
                "UPDATE artefact_sections SET state = 'generating' WHERE id = $1", sid
            )
        )
        assert await db.app_tx.run_in_tx(lambda c: state(c, sid)) == ("generating", None, None)
        # generating -> failed clears on the way out of failed
        await db.app_tx.run_in_tx(
            lambda c: c.execute(
                "UPDATE artefact_sections SET state = 'failed', failure_reason = 'inference_unavailable' WHERE id = $1",
                sid,
            )
        )
        await db.app_tx.run_in_tx(
            lambda c: c.execute(
                "UPDATE artefact_sections SET state = 'generating' WHERE id = $1", sid
            )
        )
        assert await db.app_tx.run_in_tx(lambda c: state(c, sid)) == ("generating", None, None)
        # ck_as_gap: gap without a reason violates the table constraint
        with pytest.raises(asyncpg.CheckViolationError, match="ck_as_gap"):
            await db.app_tx.run_in_tx(
                lambda c: c.execute("UPDATE artefact_sections SET state = 'gap' WHERE id = $1", sid)
            )
        # AS-7: the seeded complete section has a current version, so generating -> gap is refused
        await db.app_tx.run_in_tx(
            lambda c: c.execute(
                "UPDATE artefact_sections SET state = 'generating' WHERE id = $1", g.section
            )
        )
        with pytest.raises(asyncpg.CheckViolationError, match="returns to complete"):
            await db.app_tx.run_in_tx(
                lambda c: c.execute(
                    "UPDATE artefact_sections SET state = 'gap', gap_reason = 'insufficient_twice' WHERE id = $1",
                    g.section,
                )
            )
        # ... and returns to complete with the previous version current
        await db.app_tx.run_in_tx(
            lambda c: c.execute(
                "UPDATE artefact_sections SET state = 'complete' WHERE id = $1", g.section
            )
        )
        assert await db.app_tx.run_in_tx(lambda c: state(c, g.section)) == ("complete", None, None)


async def test_deferred_cycles_need_one_transaction(db: Db) -> None:
    """ART-2/AS-3: the pointers are checked at COMMIT; a dangling one fails the commit."""
    g = await seed_tenant(db, TENANT_A)
    with as_tenant(TENANT_A):
        with pytest.raises(asyncpg.ForeignKeyViolationError):
            await db.app_tx.run_in_tx(
                lambda c: c.execute(
                    "UPDATE artefacts SET current_version_id = $1 WHERE id = $2",
                    uuid4(),
                    g.artefact,
                )
            )
        # an immediate FK (not part of the cycles) blocks deleting a referenced row
        with pytest.raises(asyncpg.ForeignKeyViolationError):
            await db.app_tx.run_in_tx(
                lambda c: c.execute("DELETE FROM artefacts WHERE id = $1", g.artefact)
            )


# --- audit chain --------------------------------------------------------------------------


async def test_audit_chain_is_append_only_for_both_roles_and_the_table_owner(db: Db) -> None:
    """AC-1: UPDATE and DELETE are rejected by trigger (and by missing grants for DELETE)."""
    g = await seed_tenant(db, TENANT_A)
    with as_tenant(TENANT_A):
        with pytest.raises(asyncpg.CheckViolationError, match="append-only"):
            await db.app_tx.run_in_tx(
                lambda c: c.execute("UPDATE artefact_audit_chain SET action = 'x'")
            )
        with pytest.raises(asyncpg.CheckViolationError, match="append-only"):
            await db.activity_tx.run_in_tx(
                lambda c: c.execute("UPDATE artefact_audit_chain SET action = 'x'")
            )
        with pytest.raises(asyncpg.InsufficientPrivilegeError):
            await db.app_tx.run_in_tx(lambda c: c.execute("DELETE FROM artefact_audit_chain"))
    # even a superuser cannot slip a DELETE past the trigger
    conn = await db.admin()
    try:
        with pytest.raises(asyncpg.CheckViolationError, match="append-only"):
            await conn.execute(
                "DELETE FROM artefact_audit_chain WHERE artefact_id = $1", g.artefact
            )
    finally:
        await conn.close()


async def test_audit_chain_seq_is_gapless_across_rollbacks_and_hashes_recompute(db: Db) -> None:
    """AC-2..AC-4: seq 1..n with no gap even after a rolled-back writer; DB hashes == Python's."""
    art = uuid4()
    repo = PgAuditChainRepository()
    with as_tenant(TENANT_A):
        first = await db.app_tx.run_in_tx(lambda c: repo.append(c, _draft(TENANT_A, art, "e1")))
        with pytest.raises(RuntimeError):

            async def doomed(c: asyncpg.Connection) -> None:
                await repo.append(c, _draft(TENANT_A, art, "doomed"))
                raise RuntimeError("rollback")

            await db.app_tx.run_in_tx(doomed)
        second = await db.app_tx.run_in_tx(lambda c: repo.append(c, _draft(TENANT_A, art, "e2")))
        third = await db.app_tx.run_in_tx(lambda c: repo.append(c, _draft(TENANT_A, art, "e3")))
        chain = await db.app_tx.run_in_tx(lambda c: repo.list_chain(c, TENANT_A), read_only=True)
    assert first and second and third
    assert [e.seq for e in chain] == [1, 2, 3]
    assert chain[0].prev_hash is None and chain[1].prev_hash == chain[0].entry_hash
    assert (
        audit.first_break(chain) is None
    )  # the Python twin agrees with the SQL function, bit for bit
    with as_tenant(TENANT_B):
        other = await db.app_tx.run_in_tx(lambda c: repo.append(c, _draft(TENANT_B, art, "e1")))
    assert other and other.seq == 1 and other.prev_hash is None  # a chain per tenant


async def test_audit_client_cannot_supply_seq_or_hashes(db: Db) -> None:
    """AC-2: the trigger overwrites whatever the client sends."""
    art = uuid4()
    d = _draft(TENANT_A, art, "forge")
    with as_tenant(TENANT_A):
        row = await db.app_tx.run_in_tx(
            lambda c: c.fetchrow(
                "INSERT INTO artefact_audit_chain (tenant_id, seq, occurred_at, entry_type, action, "
                "actor_type, actor_id, artefact_id, source_event_type, source_event_id, metadata, "
                "payload_hash, prev_hash, entry_hash) VALUES ($1, 777, $2, $3, $4, 'user', $5, $6, "
                "$7, $8, '{}'::jsonb, '\\x00', '\\x01', '\\x02') RETURNING seq, prev_hash, entry_hash",
                TENANT_A,
                d.occurred_at,
                d.entry_type,
                d.action,
                seed.USER,
                art,
                d.source_event_type,
                d.source_event_id,
            )
        )
    assert row["seq"] == 1 and row["prev_hash"] is None and len(row["entry_hash"]) == 32


async def test_audit_duplicate_source_event_id_appends_nothing(db: Db) -> None:
    """AC-9 via ON CONFLICT DO NOTHING; no seq is burned."""
    art, repo = uuid4(), PgAuditChainRepository()
    with as_tenant(TENANT_A):
        a = await db.app_tx.run_in_tx(lambda c: repo.append(c, _draft(TENANT_A, art, "same")))
        b = await db.app_tx.run_in_tx(lambda c: repo.append(c, _draft(TENANT_A, art, "same")))
        c3 = await db.app_tx.run_in_tx(lambda c: repo.append(c, _draft(TENANT_A, art, "next")))
    assert a and b is None and c3 and c3.seq == 2


async def test_audit_concurrent_writers_serialize_into_a_gapless_chain(db: Db) -> None:
    """AC-3: the per-tenant advisory lock turns 8 racing transactions into seq 1..8."""
    art, repo = uuid4(), PgAuditChainRepository()
    with as_tenant(TENANT_A):

        async def one(i: int) -> None:
            await db.app_tx.run_in_tx(lambda c: repo.append(c, _draft(TENANT_A, art, f"race-{i}")))

        await asyncio.gather(*(one(i) for i in range(8)))
        chain = await db.app_tx.run_in_tx(lambda c: repo.list_chain(c, TENANT_A), read_only=True)
    assert [e.seq for e in chain] == list(range(1, 9))
    assert audit.first_break(chain) is None


async def test_audit_canonical_json_matches_python(db: Db) -> None:
    """The SQL canonicaliser and ``canonical_json`` agree on nesting, order, unicode and nulls."""
    doc = {"b": [1, {"z": None, "a": 'é\n"q"'}], "a": {}, "c": True, "ä": [], "A": 2}
    conn = await db.admin()
    try:
        sql = await conn.fetchval(
            "SELECT audit_canonical_json($1::jsonb)", audit.canonical_json(doc)
        )
    finally:
        await conn.close()
    assert sql == audit.canonical_json(doc)


async def test_audit_metadata_bounds_and_actor_rules(db: Db) -> None:
    """chk_audit_metadata (8 KiB, object) and chk_audit_actor."""
    art = uuid4()
    with as_tenant(TENANT_A):
        with pytest.raises(asyncpg.CheckViolationError, match="chk_audit_metadata"):
            await db.app_tx.run_in_tx(
                lambda c: c.execute(
                    "INSERT INTO artefact_audit_chain (tenant_id, occurred_at, entry_type, action, actor_type, "
                    "actor_id, artefact_id, source_event_type, source_event_id, metadata) "
                    "VALUES ($1, now(), 't', 'a', 'user', $2, $3, 't', 'big', $4::jsonb)",
                    TENANT_A,
                    seed.USER,
                    art,
                    '{"k": "' + "x" * 9000 + '"}',
                )
            )
        with pytest.raises(asyncpg.CheckViolationError, match="chk_audit_actor"):
            await db.app_tx.run_in_tx(
                lambda c: c.execute(
                    "INSERT INTO artefact_audit_chain (tenant_id, occurred_at, entry_type, action, actor_type, "
                    "actor_id, artefact_id, source_event_type, source_event_id) "
                    "VALUES ($1, now(), 't', 'a', 'anonymous', $2, $3, 't', 'anon')",
                    TENANT_A,
                    seed.USER,
                    art,
                )
            )
