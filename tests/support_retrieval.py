"""Fakes and builders for the retrieval slice's use-case and API tests."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

from rag.application.retrieval.pipeline import PipelineConfig, RetrievalPipeline
from rag.application.retrieval.ports import SearchHalfError
from rag.application.retrieval.record import RecordRetrievalSet
from rag.application.retrieval.refiner import QueryRefiner
from rag.application.retrieval.required_input import CheckRequiredInputs, RequiredInputChecker
from rag.application.retrieval.scope import ScopeSpec
from rag.application.shared.gateway import Completion, GatewayContext
from rag.domain.generation.refinement import QueryRefinement
from rag.domain.generation.retrieval import RetrievalSetView
from rag.domain.packs.profile import (
    PLATFORM_DEFAULT_BUNDLE,
    DomainProfile,
    LanguagePack,
    PackBundle,
    PackUnavailable,
    SectionTypeProfile,
)
from rag.domain.retrieval.candidate import Candidate
from rag.domain.retrieval.plan import StructuralFilters
from rag.domain.retrieval.scope import ResolvedScope, ScopedCollectionInfo
from rag.domain.retrieval.sufficiency import RequiredInputRule, SufficiencyThreshold
from rag.domain.shared.enums import GrantLevel
from rag.domain.shared.errors import DomainError, ErrorCode
from rag.domain.shared.identity import RequestIdentity

TENANT = UUID("0a3f0000-0000-7000-8000-000000000001")
USER = UUID("0a3f0000-0000-7000-8000-000000000002")
TENDER = UUID("0a3f0000-0000-7000-8000-0000000000a1")
COLL = UUID("0a3f0000-0000-7000-8000-0000000000c1")
COLL_B = UUID("0a3f0000-0000-7000-8000-0000000000c2")
NOW = datetime(2026, 4, 2, 9, 0, tzinfo=UTC)


def identity() -> RequestIdentity:
    """A caller."""
    return RequestIdentity(TENANT, USER)


def scope(
    *,
    empty: tuple[UUID, ...] = (),
    level: GrantLevel = GrantLevel.GENERATE,
    epoch: int = 0,
    ids: tuple[UUID, ...] = (COLL,),
) -> ResolvedScope:
    """A resolved scope over ``ids``; ``empty`` ones have no active generation."""
    cols = tuple(
        ScopedCollectionInfo(i, "tender", level, Decimal("1.00"), None if i in empty else 1, epoch)
        for i in ids
    )
    return ResolvedScope(
        TENANT, USER, cols, (TENDER,), level, False, "h" * 64, NOW, NOW, handle=None
    )


def chunk(
    n: int,
    *,
    role: str = "specification",
    doc: UUID | None = None,
    text: str | None = None,
    collection: UUID = COLL,
    clause: str | None = None,
    weight: str = "1.00",
) -> Candidate:
    """Candidate ``n`` with its own text and document unless given."""
    return Candidate(
        chunk_id=f"v{n}:{n}",
        document_id=doc or UUID(int=1000 + n),
        document_version=1,
        collection_id=collection,
        text=text or f"Clause {n} text about crane erection and access routes.",
        document_title=f"Doc {n}",
        document_role=role,
        language="en",
        weight=Decimal(weight),
        clause_ref=clause,
    )


def section_profile(**kw: Any) -> SectionTypeProfile:
    """A section-type profile with permissive defaults."""
    base: dict[str, Any] = dict(
        section_type="method_statement",
        candidate_depth=24,
        rerank_in=10,
        rerank_out=5,
        sufficiency=SufficiencyThreshold(0.3, 2, 0.1),
        diversity_bound=3,
        values_provisional=False,
    )
    base.update(kw)
    return SectionTypeProfile(**base)


def bundle(profile: SectionTypeProfile | None = None, **kw: Any) -> PackBundle:
    """A Tender-sourced bundle with one section-type profile ``method_statement``."""
    p = profile or section_profile()
    dp = DomainProfile(
        ref="dp-civil",
        version="dp-1",
        taxonomy_version="tax-1",
        default_profile=replace(p, section_type="*"),
        section_profiles={"method_statement": p},
        document_roles=("specification", "drawing", "bill_of_quantities"),
        **kw,
    )
    return PackBundle(dp, LanguagePack("en", "lp-1"))


class FakePacks:
    """``PackResolver``: returns ``bundle`` or raises ``PackUnavailable``."""

    def __init__(self, b: PackBundle | None = None, *, down: bool = False) -> None:
        self.bundle, self.down, self.calls = b or bundle(), down, 0

    async def resolve(self, tenant_id: UUID, **kw: Any) -> PackBundle:
        self.calls += 1
        if self.down:
            raise PackUnavailable("down")
        return self.bundle


class FakeScopeProvider:
    """``ScopeProvider``: returns ``result`` or raises ``error``; records requests."""

    def __init__(self, result: ResolvedScope | None = None, error: DomainError | None = None):
        self.result, self.error = result or scope(), error
        self.requests: list[tuple[ScopeSpec, GrantLevel]] = []

    async def resolve(self, identity: RequestIdentity, spec: ScopeSpec, required: GrantLevel):
        self.requests.append((spec, required))
        if self.error:
            raise self.error
        return self.result


class FakeGateway:
    """``ModelGateway`` + ``TokenCounter`` with scriptable failures and call counts."""

    def __init__(self) -> None:
        self.embed_error: Exception | None = None
        self.rerank_error: Exception | None = None
        self.rerank_delay = 0.0
        self.complete_error: Exception | None = None
        self.complete_text = (
            '{"intent":"topic","refined_query":"crane erection access route",'
            '"exact_terms":["crane"],"preferred_roles":["specification"]}'
        )
        self.complete_delay = 0.0
        self.rerank_scores: Callable[[list[str]], list[float]] = lambda p: [
            0.9 - i * 0.05 for i in range(len(p))
        ]
        self.embeds: list[list[str]] = []
        self.reranks: list[list[str]] = []
        self.completions: list[tuple[str, str, str]] = []
        self.token_per_char = 0.25

    async def embed(self, texts: list[str], *, ctx: GatewayContext) -> list[list[float]]:
        self.embeds.append(texts)
        if self.embed_error:
            raise self.embed_error
        return [[0.1, 0.2, 0.3] for _ in texts]

    async def rerank(
        self, query: str, passages: list[str], *, ctx: GatewayContext, deadline_seconds: float
    ) -> list[float]:
        self.reranks.append(passages)
        if self.rerank_delay:
            await asyncio.sleep(self.rerank_delay)
        if self.rerank_error:
            raise self.rerank_error
        return self.rerank_scores(passages)

    async def complete(
        self,
        task_class: str,
        system: str,
        user: str,
        *,
        ctx: GatewayContext,
        deadline_seconds: float,
    ) -> Completion:
        self.completions.append((task_class, system, user))
        if self.complete_delay:
            await asyncio.sleep(self.complete_delay)
        if self.complete_error:
            raise self.complete_error
        return Completion(self.complete_text, "m")

    async def count(self, texts: list[str], *, ctx: GatewayContext) -> list[int]:
        return [max(1, int(len(t) * self.token_per_char)) for t in texts]


class FakeDense:
    """``DenseSearch`` returning ``hits`` or raising."""

    def __init__(self, hits: list[Candidate] | None = None) -> None:
        self.hits, self.error, self.calls = hits or [], None, []

    async def search(
        self, scope: ResolvedScope, vector: list[float], *, limit: int, filters: StructuralFilters
    ) -> list[Candidate]:
        self.calls.append((limit, filters))
        if self.error:
            raise self.error
        return [replace(c, source_half="dense") for c in self.hits][:limit]


class FakeLexical:
    """``LexicalSearch`` returning ``hits`` (per query text if a dict) or raising."""

    def __init__(self, hits: list[Candidate] | None = None) -> None:
        self.hits, self.error, self.calls = hits or [], None, []

    async def search(
        self, scope: ResolvedScope, query: str, *, limit: int, filters: StructuralFilters
    ) -> list[Candidate]:
        self.calls.append((query, limit, filters))
        if self.error:
            raise self.error
        return [replace(c, source_half="lexical") for c in self.hits][:limit]


class FakeInspector:
    """``PointInspector`` over ``counts`` keyed by ``("role", x)`` / ``("entity", x)``."""

    def __init__(self, counts: dict[tuple[str, str], int] | None = None) -> None:
        self.counts, self.error, self.calls = counts or {}, None, 0

    async def count(
        self,
        scope: ResolvedScope,
        *,
        entity_type: str | None = None,
        document_role: str | None = None,
    ) -> int:
        self.calls += 1
        if self.error:
            raise self.error
        key = ("entity", entity_type) if entity_type else ("role", document_role or "")
        return self.counts.get(key, 0)


class DictKV:
    """In-memory ``KeyValueCache`` (also the ``ScopeCache`` shape)."""

    def __init__(self) -> None:
        self.data: dict[str, str] = {}

    async def get(self, key: str) -> str | None:
        return self.data.get(key)

    async def set(self, key: str, value: str, ttl_seconds: int) -> None:
        self.data[key] = value


class RecordingObserver:
    """A ``PipelineObserver`` that records every call."""

    def __init__(self) -> None:
        self.events: list[tuple[Any, ...]] = []

    @asynccontextmanager
    async def stage(self, name: str) -> AsyncIterator[None]:
        self.events.append(("stage", name))
        yield

    def marker(self, stage: str, reason: str) -> None:
        self.events.append(("marker", stage, reason))

    def verdict(self, verdict: str, section_type: str | None) -> None:
        self.events.append(("verdict", verdict, section_type))

    def rerank_skipped(self, reason: str) -> None:
        self.events.append(("rerank_skipped", reason))

    def candidate_depth(self, profile_source: str, depth: int) -> None:
        self.events.append(("depth", profile_source, depth))

    def widened_retry(self) -> None:
        self.events.append(("widened",))

    def required_input_gap(self, missing_role: str) -> None:
        self.events.append(("gap", missing_role))

    def scope_empty(self) -> None:
        self.events.append(("scope_empty",))

    def retrieval_cache(self, hit: bool) -> None:
        self.events.append(("cache", hit))

    def refiner(self, latency_seconds: float) -> None:
        self.events.append(("refiner",))

    def instruction_refiner(self, latency_seconds: float) -> None:
        self.events.append(("instruction_refiner",))

    def of(self, kind: str) -> list[tuple[Any, ...]]:
        """Events of one kind."""
        return [e for e in self.events if e[0] == kind]


class FakeTx:
    """``TransactionRunner`` handing the callback ``None``; ``fail`` makes it raise."""

    def __init__(self) -> None:
        self.fail: Exception | None = None
        self.runs = 0

    async def run_in_tx[T](
        self, fn: Callable[[Any], Awaitable[T]], *, read_only: bool = False
    ) -> T:
        self.runs += 1
        if self.fail:
            raise self.fail
        return await fn(None)


class FakeSets:
    """``RetrievalSetRepository.insert`` recorder."""

    def __init__(self) -> None:
        self.inserted: list[RetrievalSetView] = []

    async def insert(self, tx: Any, view: RetrievalSetView) -> None:
        self.inserted.append(view)


class FakeRefinements:
    """``QueryRefinementRepository`` recorder (and the order in which things were written)."""

    def __init__(self, order: list[str] | None = None) -> None:
        self.rows: list[QueryRefinement] = []
        self.order = order if order is not None else []

    async def insert(self, tx: Any, refinement: QueryRefinement) -> None:
        self.order.append("refinement")
        self.rows.append(refinement)


class Rig:
    """A fully wired pipeline over fakes."""

    def __init__(
        self,
        *,
        refiner: bool = False,
        cache: bool = False,
        config: PipelineConfig | None = None,
        packs: FakePacks | None = None,
        gateway: FakeGateway | None = None,
    ) -> None:
        self.provider = FakeScopeProvider()
        self.packs = packs or FakePacks()
        self.gw = gateway or FakeGateway()
        self.dense = FakeDense([chunk(1), chunk(2), chunk(3)])
        self.lexical = FakeLexical([chunk(2), chunk(4)])
        self.inspector = FakeInspector()
        self.kv = DictKV() if cache else None
        self.obs = RecordingObserver()
        self.tx = FakeTx()
        self.order: list[str] = []
        self.sets = FakeSets()
        self.refinements = FakeRefinements(self.order)
        self.checker = RequiredInputChecker(self.inspector, self.obs)
        self.refiner = QueryRefiner(
            self.gw, self.kv, enabled=refiner, deadline_seconds=0.15, observer=self.obs
        )
        self.pipeline = RetrievalPipeline(
            self.provider,
            self.packs,
            self.gw,
            self.gw,
            self.dense,
            self.lexical,
            self.refiner,
            self.checker,
            self.kv,
            config or PipelineConfig(),
            self.obs,
        )
        self.recorder = RecordRetrievalSet(self.tx, self.sets, self.refinements)
        self.required = CheckRequiredInputs(self.provider, self.packs, self.checker, self.obs)


__all__ = ["PLATFORM_DEFAULT_BUNDLE", "RequiredInputRule", "SearchHalfError", "ErrorCode", "uuid4"]
