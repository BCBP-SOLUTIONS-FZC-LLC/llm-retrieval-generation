"""Adapters from ``corpusscope`` to the application's scope ports, and the Qdrant point counter."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID

from rag.application.retrieval.scope import ScopeSpec
from rag.application.shared.scope import ScopeTarget
from rag.corpusscope import (
    Level,
    Principal,
    QdrantScopedReader,
    ScopedCollection,
    ScopeLevelInsufficient,
    ScopeNotFound,
    ScopeRequest,
    ScopeResolver,
    ScopeStoreUnavailable,
    ScopeToken,
)
from rag.corpusscope import ScopeError as CorpusScopeError
from rag.domain.corpus.collection import Collection
from rag.domain.retrieval.scope import ResolvedScope, ScopedCollectionInfo
from rag.domain.shared.enums import GrantLevel
from rag.domain.shared.errors import DomainError, ErrorCode
from rag.domain.shared.identity import RequestIdentity


def to_principal(identity: RequestIdentity) -> Principal:
    """Gateway-asserted identity as the vendored library's ``Principal``."""
    return Principal(
        tenant_id=identity.tenant_id,
        principal_id=identity.principal_id,
        roles=identity.roles,
        departments=tuple(d.department_id for d in identity.departments),
    )


def to_domain_error(exc: CorpusScopeError) -> DomainError:
    """§24 mapping. A missing grant is 404 and never 403 (CG-8, §6.2)."""
    if isinstance(exc, ScopeNotFound):
        return DomainError(ErrorCode.COLLECTION_NOT_FOUND, "No collection is available in scope.")
    if isinstance(exc, ScopeLevelInsufficient):
        return DomainError(ErrorCode.GRANT_LEVEL_INSUFFICIENT, "The grant level is insufficient.")
    if isinstance(exc, ScopeStoreUnavailable):
        return DomainError(ErrorCode.SCOPE_STORE_UNAVAILABLE, "Grant state cannot be read.")
    return DomainError(ErrorCode.SCOPE_STORE_UNAVAILABLE, "Scope resolution failed.")


def to_resolved(token: ScopeToken) -> ResolvedScope:
    """The application's view of a token; the token itself rides as the opaque ``handle``."""
    return ResolvedScope(
        tenant_id=token.tenant_id,
        principal_id=token.principal_id,
        collections=tuple(
            ScopedCollectionInfo(
                c.id,
                c.type,
                GrantLevel(c.level.label),
                c.weight,
                c.index_generation,
                c.purge_epoch,
            )
            for c in token.collections
        ),
        tender_ids=token.tender_ids,
        level=GrantLevel(token.level.label),
        clamped=token.clamped,
        resolution_hash=token.resolution_hash,
        resolved_at=token.resolved_at,
        expires_at=token.expires_at,
        handle=token,
    )


class CorpusScopeAdapter:
    """The corpus ``ScopeResolver``, shared ``ScopeAuthorizer`` and retrieval ``ScopeProvider``.

    All three ride one ``corpusscope.ScopeResolver``.
    """

    def __init__(self, resolver: ScopeResolver) -> None:
        self._resolver = resolver

    async def _resolve(
        self, identity: RequestIdentity, request: ScopeRequest, required: Level | None
    ) -> ScopeToken:
        try:
            return await self._resolver.resolve(to_principal(identity), request, required=required)
        except CorpusScopeError as exc:
            raise to_domain_error(exc) from exc

    async def visible_collection_ids(
        self, identity: RequestIdentity, *, only: UUID | None = None
    ) -> tuple[UUID, ...]:
        """Corpus ``ScopeResolver``: ids in scope; an empty or ungranted scope is ``()`` (CG-8)."""
        request = ScopeRequest(collection_ids=() if only is None else (only,))
        try:
            token = await self._resolve(identity, request, None)
        except DomainError as exc:
            if exc.code is ErrorCode.COLLECTION_NOT_FOUND:
                return ()
            raise
        return tuple(c.id for c in token.collections)

    async def require_level(
        self, identity: RequestIdentity, target: ScopeTarget, level: GrantLevel
    ) -> None:
        """``ScopeAuthorizer``: re-resolve on every read (API-10) and check the level."""
        request = ScopeRequest(target.tender_id, target.collection_ids)
        await self._resolve(identity, request, Level.parse(level.value))

    async def resolve(
        self, identity: RequestIdentity, spec: ScopeSpec, required: GrantLevel
    ) -> ResolvedScope:
        """Retrieval ``ScopeProvider``."""
        request = ScopeRequest(spec.tender_id, spec.collection_ids, spec.collection_types)
        return to_resolved(await self._resolve(identity, request, Level.parse(required.value)))


class QdrantPointCounter:
    """``PointCounter`` for C-I8: points of the source tender in its physical collection."""

    def __init__(
        self,
        reader: QdrantScopedReader,
        *,
        timeout_seconds: float = 10.0,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._reader, self._timeout, self._clock = reader, timeout_seconds, clock

    async def count_tender_points(self, source: Collection) -> int:
        """Current points of ``source`` at its active generation, via a one-collection token."""
        now = self._clock()
        scoped = ScopedCollection(
            id=source.id,
            type=source.type.value,
            level=Level.SEARCH,
            weight=Decimal("1.00"),
            physical_collection=source.physical_collection,
            shard_key=source.shard_key,
            tenant_id=source.tenant_id,
            tender_id=source.tender_id,
            index_generation=source.active_index_generation,
            purge_epoch=source.purge_epoch,
        )
        token = ScopeToken.create(
            tenant_id=source.tenant_id,
            principal_id=None,
            collections=(scoped,),
            level=Level.SEARCH,
            clamped=False,
            resolved_at=now,
            expires_at=now + timedelta(seconds=60),
        )
        try:
            return await asyncio.wait_for(
                self._reader.count(token, source.id, now=now), self._timeout
            )
        except Exception as exc:
            raise DomainError(
                ErrorCode.SCOPE_STORE_UNAVAILABLE, "The vector store reader is not available."
            ) from exc
