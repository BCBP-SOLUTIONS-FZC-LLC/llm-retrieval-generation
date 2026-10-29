"""Wiring of ``corpusscope``: the resolver and its adapters, fail-closed where config is absent."""

from __future__ import annotations

from dataclasses import dataclass

from rag.application.corpus.ports import PointCounter
from rag.application.shared.ports import TransactionRunner
from rag.corpusscope import QdrantScopedReader, ScopeResolver, TenderAccess
from rag.infrastructure.cache.valkey import ValkeyCache, build_cache
from rag.infrastructure.config import Settings
from rag.infrastructure.corpus_placeholders import UnavailablePointCounter
from rag.infrastructure.iam.tac4 import HttpTenderAccess, UnconfiguredTenderAccess
from rag.infrastructure.scope.adapter import CorpusScopeAdapter, QdrantPointCounter
from rag.infrastructure.scope.store import PgCollectionStore


@dataclass(slots=True)
class ScopeServices:
    """The scope adapter (resolver, authorizer, provider in one), the Qdrant reader, the cache.

    ``reader`` is ``None`` where ``QDRANT_URL`` is empty, and the TAC-4 client refuses where
    ``IAM_BASE_URL`` is empty: scope-dependent paths then fail closed (CG-4), never default.
    """

    adapter: CorpusScopeAdapter
    points: PointCounter
    reader: QdrantScopedReader | None
    cache: ValkeyCache | None

    async def aclose(self) -> None:
        """Close the Valkey and Qdrant clients."""
        if self.cache is not None:
            await self.cache.aclose()
        if self.reader is not None:
            await self.reader.aclose()


def build_scope_services(settings: Settings, corpus_tx: TransactionRunner) -> ScopeServices:
    """Construct the resolver over ``llm_corpus``, TAC-4, Valkey (60 s) and Qdrant."""
    cache = build_cache(settings)
    tac4: TenderAccess = (
        HttpTenderAccess(settings.tender_acl_url(), settings.grant_read_timeout_ms / 1000)
        if settings.tender_acl_url()
        else UnconfiguredTenderAccess()
    )
    resolver = ScopeResolver(
        store=PgCollectionStore(corpus_tx),
        tac4=tac4,
        cache=cache,
        token_ttl_seconds=settings.scope_token_ttl_seconds,
        cache_ttl_seconds=settings.scope_cache_ttl_seconds,
        store_timeout_seconds=settings.grant_read_timeout_ms / 1000,
    )
    reader = (
        QdrantScopedReader.connect(settings.qdrant_url, settings.qdrant_api_key or None)
        if settings.qdrant_url
        else None
    )
    points: PointCounter = (
        QdrantPointCounter(reader) if reader is not None else UnavailablePointCounter()
    )
    return ScopeServices(CorpusScopeAdapter(resolver), points, reader, cache)
