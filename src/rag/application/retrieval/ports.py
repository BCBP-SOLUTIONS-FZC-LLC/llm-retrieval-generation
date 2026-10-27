"""Outbound ports of the retrieval module (besides scope, packs and the Gateway)."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from typing import Protocol

from rag.domain.retrieval.candidate import Candidate
from rag.domain.retrieval.plan import StructuralFilters
from rag.domain.retrieval.scope import ResolvedScope


class SearchHalfError(Exception):
    """One search half (dense or lexical) is unavailable: breaker open, timeout, store down."""


class DenseSearch(Protocol):
    """Qdrant dense half. All filters are payload predicates built from the token (§7.4)."""

    async def search(
        self,
        scope: ResolvedScope,
        vector: list[float],
        *,
        limit: int,
        filters: StructuralFilters,
    ) -> list[Candidate]:
        """Best-first candidates across the scope's searchable collections (``source_half`` set).

        Raises ``SearchHalfError`` (200 ms, no retry, breaker; §14.3).
        """
        ...


class LexicalSearch(Protocol):
    """Postgres lexical half over ``llm_corpus`` chunk rows, read-only (DEP-5)."""

    async def search(
        self,
        scope: ResolvedScope,
        query: str,
        *,
        limit: int,
        filters: StructuralFilters,
    ) -> list[Candidate]:
        """Best-first candidates. ``query`` follows web-search syntax: quoted text is a phrase.

        Raises ``SearchHalfError`` (200 ms, no retry, breaker).
        """
        ...


class PointInspector(Protocol):
    """Scoped counts the required-input check needs (§6.9, LD-100)."""

    async def count(
        self,
        scope: ResolvedScope,
        *,
        entity_type: str | None = None,
        document_role: str | None = None,
    ) -> int:
        """Current points of that type or role across the scope. Raises ``SearchHalfError``."""
        ...


class KeyValueCache(Protocol):
    """Tenant-prefixed string cache (§11.1). A failing cache is a miss, never an error."""

    async def get(self, key: str) -> str | None:
        """The value or ``None``."""
        ...

    async def set(self, key: str, value: str, ttl_seconds: int) -> None:
        """Store with a TTL."""
        ...


class PipelineObserver(Protocol):
    """§16.2 instrumentation. Labels are bounded vocabularies; never ids or content (OBS-1)."""

    def stage(self, name: str) -> AbstractAsyncContextManager[None]:
        """Time one pipeline stage (``llm_..._retrieval_stage_duration_seconds{stage}``)."""
        ...

    def marker(self, stage: str, reason: str) -> None:
        """One degradation marker."""
        ...

    def verdict(self, verdict: str, section_type: str | None) -> None:
        """One retrieval verdict (and the sufficiency counter)."""
        ...

    def rerank_skipped(self, reason: str) -> None:
        """The reranker was skipped or failed."""
        ...

    def candidate_depth(self, profile_source: str, depth: int) -> None:
        """Depth before rerank, as a band counter."""
        ...

    def widened_retry(self) -> None:
        """D-16's single retry ran."""
        ...

    def required_input_gap(self, missing_role: str) -> None:
        """A role a required-input check found missing."""
        ...

    def scope_empty(self) -> None:
        """A scope resolved nothing (404)."""
        ...

    def retrieval_cache(self, hit: bool) -> None:
        """A retrieval-set cache lookup."""
        ...

    def refiner(self, latency_seconds: float) -> None:
        """A refiner call's latency."""
        ...

    def instruction_refiner(self, latency_seconds: float) -> None:
        """An Instruction Refiner call's latency (same metric, ``refiner="instruction"``)."""
        ...


class NullObserver:
    """The observer when metrics are off (the default until OQ-103 registers ``llm``)."""

    @asynccontextmanager
    async def stage(self, name: str) -> AsyncIterator[None]:
        """No timing."""
        yield

    def marker(self, stage: str, reason: str) -> None:
        """No-op."""

    def verdict(self, verdict: str, section_type: str | None) -> None:
        """No-op."""

    def rerank_skipped(self, reason: str) -> None:
        """No-op."""

    def candidate_depth(self, profile_source: str, depth: int) -> None:
        """No-op."""

    def widened_retry(self) -> None:
        """No-op."""

    def required_input_gap(self, missing_role: str) -> None:
        """No-op."""

    def scope_empty(self) -> None:
        """No-op."""

    def retrieval_cache(self, hit: bool) -> None:
        """No-op."""

    def refiner(self, latency_seconds: float) -> None:
        """No-op."""

    def instruction_refiner(self, latency_seconds: float) -> None:
        """No-op."""
