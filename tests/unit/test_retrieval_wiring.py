"""Wiring and the Qdrant-backed adapters over a stub reader: fail-closed config, merge, breaker."""

from __future__ import annotations

from dataclasses import replace
from decimal import Decimal
from typing import Any
from uuid import uuid4

import pytest
from tests.api.test_lifespan import FakePool
from tests.support_scope import T0, TENANT, principal, resolver, row

from rag.application.retrieval.ports import SearchHalfError
from rag.corpusscope import DenseHit, ScopeRequest, ScopeStoreUnavailable
from rag.domain.corpus.collection import Collection
from rag.domain.retrieval.plan import StructuralFilters
from rag.domain.shared.errors import DomainError, ErrorCode
from rag.infrastructure.config import Settings
from rag.infrastructure.corpus_placeholders import UnavailablePointCounter
from rag.infrastructure.resilience import CircuitBreaker
from rag.infrastructure.retrieval.qdrant import QdrantDenseSearch, QdrantPointInspector
from rag.infrastructure.scope.adapter import CorpusScopeAdapter, QdrantPointCounter, to_resolved
from rag.presentation.dependencies import build_container
from rag.presentation.scope_dependencies import build_scope_services


class StubReader:
    """Stands in for ``QdrantScopedReader``: scripted hits and counts, one error switch."""

    def __init__(self) -> None:
        self.hits: dict[Any, list[DenseHit]] = {}
        self.counts: dict[tuple[str | None, str | None], int] = {}
        self.error: Exception | None = None
        self.calls: list[dict[str, Any]] = []

    async def dense_search(self, token, cid, vector, limit, **kw):  # type: ignore[no-untyped-def]
        self.calls.append({"cid": cid, "limit": limit, **kw})
        if self.error:
            raise self.error
        return self.hits.get(cid, [])

    async def count(self, token, cid, *, now, entity_type=None, document_role=None):  # type: ignore[no-untyped-def]
        if self.error:
            raise self.error
        return self.counts.get((entity_type, document_role), 0)


def payload(n: int, **kw: Any) -> dict[str, Any]:
    """A complete point payload."""
    base = {
        "chunk_id": f"v{n}:{n}",
        "document_id": str(uuid4()),
        "document_version": 1,
        "text": f"text {n}",
        "document_title": f"T{n}",
        "document_role": "specification",
        "language": "en",
    }
    return base | kw


async def scope_of(*rows):  # type: ignore[no-untyped-def]
    """A ResolvedScope (with its real token as handle) over ``rows``."""
    r, _ = resolver(list(rows))
    return to_resolved(await r.resolve(principal("member"), ScopeRequest()))


# --- fail-closed configuration ---------------------------------------------------------------


def test_empty_qdrant_url_leaves_the_counter_unavailable_and_no_reader() -> None:
    """Absent Qdrant config fails closed: C-I8 complete is 503, searches have no dense half."""
    s = build_scope_services(Settings(qdrant_url="", valkey_url=""), FakePool("c", []))  # type: ignore[arg-type]
    assert s.reader is None and s.cache is None
    assert isinstance(s.points, UnavailablePointCounter)


async def test_empty_iam_url_refuses_a_named_restricted_tender_but_not_other_requests() -> None:
    """Absent TAC-4 config is CG-4 for the restricted path only."""
    s = build_scope_services(Settings(iam_base_url=""), FakePool("c", []))  # type: ignore[arg-type]
    restricted = row("tender", restricted=True)
    other = row("tenant")
    resolved = s.adapter._resolver
    resolved.store = resolver([restricted, other])[1]  # type: ignore[misc]
    with pytest.raises(ScopeStoreUnavailable):
        await resolved.resolve(principal("member"), ScopeRequest(tender_id=restricted.tender_id))
    assert (await resolved.resolve(principal("member"), ScopeRequest())).collections


def test_the_container_wires_the_real_scope_adapter_everywhere() -> None:
    """Unwired* are replaced: corpus reads, generation reads and retrieval share one adapter."""
    c = build_container(Settings(), FakePool("c", []), FakePool("g", []))  # type: ignore[arg-type]
    adapter = c.scope.adapter
    assert isinstance(adapter, CorpusScopeAdapter)
    assert c.generation.get_artefact.scope is adapter
    assert c.generation.get_retrieval_set.scope is adapter
    assert c.corpus.list_collections._scope is adapter
    assert c.retrieval.pipeline._scope is adapter
    assert isinstance(c.scope.points, QdrantPointCounter)
    assert c.retrieval.gateway is c.retrieval.tokens  # one Gateway client for embed/rerank/tokens


# --- QdrantPointCounter (C-I8) ---------------------------------------------------------------


async def test_point_counter_counts_the_source_tenders_current_points() -> None:
    """C-I8: a one-collection token; a reader failure is 503, never a guessed count."""
    reader = StubReader()
    reader.counts[(None, None)] = 41
    source = Collection.create(tenant_id=TENANT, tender_id=uuid4(), restricted=False).activate()
    counter = QdrantPointCounter(reader, clock=lambda: T0)  # type: ignore[arg-type]
    assert await counter.count_tender_points(source) == 41
    reader.error = ConnectionError("down")
    with pytest.raises(DomainError) as e:
        await counter.count_tender_points(source)
    assert e.value.code is ErrorCode.SCOPE_STORE_UNAVAILABLE


# --- dense search and counts -----------------------------------------------------------------


async def test_dense_search_merges_collections_by_score_and_applies_weights_and_filters() -> None:
    """One search per searchable collection; best score first; weight from the scope."""
    a, b = row("tender"), row("tenant")
    scope = await scope_of(a, b)
    reader = StubReader()
    ca, cb = (c.id for c in scope.collections)
    reader.hits = {
        ca: [DenseHit("1", 0.5, payload(1))],
        cb: [DenseHit("2", 0.9, payload(2)), DenseHit("3", 0.1, {"text": "no role"})],
    }
    search = QdrantDenseSearch(reader, CircuitBreaker(), clock=lambda: T0)  # type: ignore[arg-type]
    hits = await search.search(
        scope, [0.1], limit=5, filters=StructuralFilters(("specification",), "en")
    )
    assert [h.chunk_id for h in hits] == ["v2:2", "v1:1"]  # the malformed point is dropped
    assert {h.collection_id: h.weight for h in hits} == {
        cb: Decimal("0.90"),
        ca: Decimal("1.00"),
    }
    assert all(
        c["document_roles"] == ("specification",) and c["language"] == "en" for c in reader.calls
    )
    assert len(reader.calls) == 2


async def test_dense_search_skips_empty_collections() -> None:
    """SCOPE-12: no search is issued against a collection with no generation."""
    scope = await scope_of(row("tender", generation=None), row("tenant"))
    reader = StubReader()
    await QdrantDenseSearch(reader, CircuitBreaker()).search(  # type: ignore[arg-type]
        scope, [0.1], limit=5, filters=StructuralFilters()
    )
    assert len(reader.calls) == 1


async def test_dense_failure_and_missing_reader_are_search_half_errors_and_the_breaker_opens() -> (
    None
):
    """§14.3: any failure degrades; five open the shared breaker; no reader configured fails."""
    scope = await scope_of(row("tender"))
    reader = StubReader()
    reader.error = RuntimeError("qdrant down")
    breaker = CircuitBreaker()
    search = QdrantDenseSearch(reader, breaker)  # type: ignore[arg-type]
    for _ in range(5):
        with pytest.raises(SearchHalfError):
            await search.search(scope, [0.1], limit=5, filters=StructuralFilters())
    assert breaker.is_open
    seen = len(reader.calls)
    with pytest.raises(SearchHalfError):
        await search.search(scope, [0.1], limit=5, filters=StructuralFilters())
    assert len(reader.calls) == seen  # no call while open
    with pytest.raises(SearchHalfError):
        await QdrantDenseSearch(None, CircuitBreaker()).search(
            scope, [0.1], limit=5, filters=StructuralFilters()
        )


async def test_the_entity_count_shares_the_search_breaker_and_never_guesses() -> None:
    """§14.3 / LD-100: counts sum across collections; failure is SearchHalfError."""
    scope = await scope_of(row("tender"), row("tenant"))
    reader = StubReader()
    reader.counts[("standard", None)] = 3
    breaker = CircuitBreaker()
    inspector = QdrantPointInspector(reader, breaker)  # type: ignore[arg-type]
    assert await inspector.count(scope, entity_type="standard") == 6  # 3 per searchable collection
    assert await inspector.count(scope, document_role="drawing") == 0
    reader.error = RuntimeError("down")
    for _ in range(5):
        with pytest.raises(SearchHalfError):
            await inspector.count(scope, entity_type="standard")
    assert breaker.is_open


async def test_a_scope_without_a_token_cannot_search() -> None:
    """The adapters need the corpusscope token: a bare ResolvedScope has none."""
    scope = replace(await scope_of(row("tender")), handle=None)
    with pytest.raises(SearchHalfError):
        await QdrantDenseSearch(StubReader(), CircuitBreaker()).search(  # type: ignore[arg-type]
            scope, [0.1], limit=1, filters=StructuralFilters()
        )
