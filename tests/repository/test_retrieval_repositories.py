# ruff: noqa: E501 (SQL literals)
"""Retrieval persistence and reads against real Postgres: set insert, scope store, lexical search.

Needs Docker (or ``RAG_TEST_PG_DSN_BASE``); skipped otherwise. NEVER RUN in the slice that wrote
it (no Docker there): treat a first failure as a test bug as likely as a code bug.

Ingestion's chunk table is not this repo's (DEP-5), so the lexical test creates a stub shaped
like the assumption documented in ``infrastructure/retrieval/lexical.py``, owned by the
superuser, with a ``SELECT`` grant to ``rag_app`` only.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import asyncpg
import pytest
from pgcommon.migrate import Runner
from tests.repository.support import Db, as_tenant
from tests.repository.test_corpus_repositories import REPOS, AppTx
from tests.support_corpus import NOW, TENANT, TENDER

from rag.corpusscope import (
    PLATFORM,
    REFERENCE,
    Level,
    ScopeRequest,
    ScopeResolver,
    ScopeToken,
)
from rag.corpusscope.model import Principal
from rag.domain.corpus.collection import Collection
from rag.domain.corpus.index_generation import IndexGeneration
from rag.domain.generation.refinement import QueryRefinement
from rag.domain.generation.retrieval import RetrievalChunk, RetrievalSet, RetrievalSetView
from rag.domain.retrieval.plan import StructuralFilters
from rag.domain.shared.enums import (
    CollectionStatus,
    GrantLevel,
    RefinerOutcome,
    RetrievalVerdict,
)
from rag.infrastructure.persistence.generation.repositories import (
    PgQueryRefinementRepository,
    PgRetrievalSetRepository,
)
from rag.infrastructure.resilience import CircuitBreaker
from rag.infrastructure.retrieval.lexical import PgLexicalSearch
from rag.infrastructure.scope.store import PgCollectionStore

pytestmark = pytest.mark.integration

MIGRATIONS = Path(__file__).resolve().parents[2] / "migrations"
OTHER = UUID("0a3f0000-0000-7000-8000-000000000009")


# --- generation database: the set insert ---------------------------------------------------------


def view(tenant: UUID, set_id: UUID, *, widened_of: UUID | None = None) -> RetrievalSetView:
    """A small recorded set with two chunks (one from a reference collection)."""
    s = RetrievalSet(
        id=set_id,
        tenant_id=tenant,
        verdict=RetrievalVerdict.INSUFFICIENT,
        verdict_reason="top_score_below_threshold",
        scope_resolution_hash="h" * 64,
        grant_level_applied=GrantLevel.ANSWER,
        filter_set={"is_current": True},
        index_generations={"c": 1},
        profile_source="section_type",
        applied_profile={"rerank_out": 5},
        reranker_ran=False,
        budget_allocation={"evidence_tokens": 1},
        domain_profile_version="dp-1",
        language_pack_version="lp-1",
        tender_id=TENDER,
        widened_retry_of=widened_of,
        degradation_markers=[
            {"stage": "rerank", "reason": "deadline_exceeded", "applied_fallback": "fused_order"}
        ],
    )
    chunks = tuple(
        RetrievalChunk(
            retrieval_set_id=set_id,
            tenant_id=tenant,
            rank=i,
            chunk_id=f"v:{i}",
            document_id=uuid4(),
            document_version=1,
            collection_id=uuid4(),
            collection_weight=Decimal("0.90"),
            fused_score=Decimal("0.0312"),
            source_half="both",
            document_title="T",
            document_role="specification",
            language="en",
            excerpt="An excerpt.",
            rerank_score=None,
        )
        for i in (1, 2)
    )
    return RetrievalSetView(s, chunks, ())


async def test_set_insert_round_trips_and_a_retry_names_its_first(db: Db) -> None:
    """RS-1/RS-6: the set and its chunks persist; ``widened_retry_of`` is a real FK."""
    first, second = uuid4(), uuid4()
    repo = PgRetrievalSetRepository()
    with as_tenant(TENANT):
        await db.app_tx.run_in_tx(lambda tx: repo.insert(tx, view(TENANT, first)))
        await db.app_tx.run_in_tx(
            lambda tx: repo.insert(tx, view(TENANT, second, widened_of=first))
        )
        got = await db.app_tx.run_in_tx(lambda tx: repo.get_view(tx, second), read_only=True)
    assert got is not None and got.retrieval_set.widened_retry_of == first
    assert got.retrieval_set.degradation_markers[0]["stage"] == "rerank"
    assert [c.rank for c in got.chunks] == [1, 2] and got.chunks[0].collection_weight == Decimal(
        "0.90"
    )


async def test_another_tenant_cannot_read_or_insert_into_the_set(db: Db) -> None:
    """RLS: the row is invisible to B, and B cannot write a row stamped with A's tenant."""
    set_id = uuid4()
    repo = PgRetrievalSetRepository()
    with as_tenant(TENANT):
        await db.app_tx.run_in_tx(lambda tx: repo.insert(tx, view(TENANT, set_id)))
    with as_tenant(OTHER):
        assert (
            await db.app_tx.run_in_tx(lambda tx: repo.get_view(tx, set_id), read_only=True) is None
        )
        with pytest.raises(asyncpg.PostgresError):
            await db.app_tx.run_in_tx(lambda tx: repo.insert(tx, view(TENANT, uuid4())))


async def test_refinement_row_insert_defaults_user_action_to_unknown(db: Db) -> None:
    """§4.6.1: one row per refiner call; ``user_action`` stays ``unknown``."""
    r = QueryRefinement(
        id=uuid4(),
        tenant_id=TENANT,
        principal_hash="a" * 64,
        original_query="q",
        pack_version="dp-1",
        refiner_version="qr-1",
        latency_ms=12,
        outcome=RefinerOutcome.DEGRADED,
    )
    with as_tenant(TENANT):
        await db.app_tx.run_in_tx(lambda tx: PgQueryRefinementRepository().insert(tx, r))
        row = await db.app_tx.run_in_tx(
            lambda tx: tx.fetchrow("SELECT * FROM query_refinements WHERE id = $1", r.id),
            read_only=True,
        )
    assert row["user_action"] == "unknown" and row["outcome"] == "degraded"
    assert row["refined_query"] is None and list(row["exact_terms"]) == []


# --- corpus database: scope store and lexical ----------------------------------------------------


@pytest.fixture
async def dsn(dsn_base: str) -> AsyncIterator[str]:
    """A fresh migrated ``llm_corpus`` plus a stub chunk table (test-only DDL)."""
    admin = await asyncpg.connect(f"{dsn_base}/postgres")
    await admin.execute("DROP DATABASE IF EXISTS llm_corpus_retrieval")
    await admin.execute("CREATE DATABASE llm_corpus_retrieval")
    await admin.close()
    url = f"{dsn_base}/llm_corpus_retrieval"
    from eventcommon.outbox import apply_schema  # corpus 0003 grants on it: outbox goes first
    from pgcommon import Config, Pool

    boot = await Pool.create(Config(dsn=url, max_conns=1, min_conns=1))
    await apply_schema(boot)
    await boot.drain_and_close()
    await Runner(url, MIGRATIONS / "corpus").up()
    conn = await asyncpg.connect(url)
    await conn.execute(
        """
        CREATE TABLE document_chunks (
          chunk_id text PRIMARY KEY, tenant_id uuid NOT NULL, tender_id uuid NOT NULL,
          collection_id uuid NOT NULL, index_generation int NOT NULL, is_current boolean NOT NULL,
          document_id uuid NOT NULL, document_version int NOT NULL, document_title text NOT NULL,
          document_role text NOT NULL, language text NOT NULL, heading_path text, clause_ref text,
          parent_chunk_id text, text text NOT NULL, tsv tsvector NOT NULL);
        ALTER TABLE document_chunks ENABLE ROW LEVEL SECURITY;
        ALTER TABLE document_chunks FORCE ROW LEVEL SECURITY;
        CREATE POLICY tenant_read ON document_chunks FOR SELECT USING (
          tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid
          OR tenant_id = '00000000-0000-7000-8000-4c4c4d000003'::uuid);
        GRANT SELECT ON document_chunks TO rag_app;
        """
    )
    await conn.close()
    yield url


async def seed_collection(
    dsn: str, tenant: UUID, tender: UUID, *, active: bool = True
) -> Collection:
    """A collection and its generation 1, through the real repositories."""
    c = Collection.create(tenant_id=tenant, tender_id=tender, restricted=False)
    if active:
        c = c.activate()

    async def work(tx: Any) -> None:
        await REPOS.collections.insert(tx, c)
        await REPOS.generations.insert(
            tx,
            IndexGeneration.first(
                collection_id=c.id, tenant_id=c.tenant_id, active=active, now=NOW
            ),
        )

    await AppTx(dsn, c.tenant_id).run_in_tx(work)
    return c


async def test_scope_store_returns_own_and_reference_rows_with_their_generations(dsn: str) -> None:
    """§6.2 steps 2, 6: any status comes back (the resolver filters); generation from the join."""
    mine = await seed_collection(dsn, TENANT, TENDER)
    provisioning = await seed_collection(dsn, TENANT, uuid4(), active=False)
    await seed_collection(dsn, OTHER, TENDER)
    ref = Collection.create(
        tenant_id=TENANT,
        tender_id=REFERENCE,
        restricted=False,
    )

    async def put_ref(tx: Any) -> None:
        await REPOS.collections.insert(tx, ref)

    await AppTx(dsn, PLATFORM).run_in_tx(put_ref)
    rows = {r.id: r for r in await PgCollectionStore(AppTx(dsn, TENANT)).load(TENANT)}
    assert set(rows) == {mine.id, provisioning.id, ref.id}  # never OTHER's
    assert rows[mine.id].active_generation == 1 and rows[mine.id].status == "active"
    assert rows[provisioning.id].active_generation is None  # no active generation: empty corpus
    assert rows[ref.id].type == "reference" and rows[ref.id].tenant_id == PLATFORM


async def test_resolver_over_the_real_store_admits_active_only(dsn: str) -> None:
    """COL-10/SCOPE-11 end to end on real rows (ISO-9)."""
    active = await seed_collection(dsn, TENANT, TENDER)
    hidden = await seed_collection(dsn, TENANT, uuid4(), active=False)
    assert hidden.status is CollectionStatus.PROVISIONING

    class NoTac:
        async def level(self, *a: Any) -> str | None:
            return None

    resolver = ScopeResolver(
        PgCollectionStore(AppTx(dsn, TENANT)), NoTac(), store_timeout_seconds=5
    )
    principal = Principal(TENANT, uuid4(), frozenset({"member"}), ())
    # a tenant-wide or reference collection is absent here and the tender needs a department,
    # so a member without one resolves nothing: SCOPE-6, never a fallback
    with pytest.raises(Exception, match="nothing is in scope"):
        await resolver.resolve(principal, ScopeRequest())
    assert active.status is CollectionStatus.ACTIVE


async def insert_chunk(
    dsn: str,
    c: Collection,
    n: int,
    text: str,
    *,
    generation: int = 1,
    current: bool = True,
    tenant: UUID | None = None,
) -> None:
    conn = await asyncpg.connect(dsn)
    await conn.execute(
        "INSERT INTO document_chunks VALUES ($1,$2,$3,$4,$5,$6,$7,1,'Doc','specification','en',NULL,NULL,NULL,$8,to_tsvector('simple',$8))",
        f"v{n}:{n}",
        tenant or c.tenant_id,
        c.tender_id,
        c.id,
        generation,
        current,
        uuid4(),
        text,
    )
    await conn.close()


def token_for(c: Collection) -> ScopeToken:
    """A one-collection token for ``c`` (as the C-I8 counter builds one)."""
    from datetime import timedelta

    from rag.corpusscope import ScopedCollection

    sc = ScopedCollection(
        c.id,
        c.type.value,
        Level.SEARCH,
        Decimal("1.00"),
        c.physical_collection,
        c.shard_key,
        c.tenant_id,
        c.tender_id,
        c.active_index_generation,
        c.purge_epoch,
    )
    return ScopeToken.create(
        tenant_id=c.tenant_id,
        principal_id=None,
        collections=(sc,),
        level=Level.SEARCH,
        clamped=False,
        resolved_at=NOW,
        expires_at=NOW + timedelta(seconds=60),
    )


async def test_lexical_search_applies_the_whole_compound_and_is_current(dsn: str) -> None:
    """SCOPE-2/RET-8 on the lexical half: other tenant, other generation, superseded: none match."""
    from rag.domain.retrieval.scope import ResolvedScope, ScopedCollectionInfo

    mine = await seed_collection(dsn, TENANT, TENDER)
    theirs = await seed_collection(dsn, OTHER, TENDER)
    await insert_chunk(dsn, mine, 1, "crane erection access route")
    await insert_chunk(dsn, mine, 2, "crane erection superseded", current=False)
    await insert_chunk(dsn, mine, 3, "crane erection old generation", generation=2)
    await insert_chunk(dsn, theirs, 4, "crane erection of another tenant")
    token = token_for(mine)
    scope = ResolvedScope(
        TENANT,
        None,
        (ScopedCollectionInfo(mine.id, "tender", GrantLevel.SEARCH, Decimal("1.00"), 1, 0),),
        (TENDER,),
        GrantLevel.SEARCH,
        False,
        token.resolution_hash,
        NOW,
        NOW,
        handle=token,
    )
    search = PgLexicalSearch(AppTx(dsn, TENANT), "document_chunks", CircuitBreaker())
    hits = await search.search(scope, "crane erection", limit=10, filters=StructuralFilters())
    assert [h.chunk_id for h in hits] == ["v1:1"] and hits[0].source_half == "lexical"
    none = await search.search(scope, "crane", limit=10, filters=StructuralFilters(("drawing",)))
    assert none == []
