"""P-1, P-2 and P-3: reads through the scope resolver (§5.7.15)."""

from __future__ import annotations

from rag.application.corpus.commands import CollectionQuery, ListCollectionsQuery, ListGrantsQuery
from rag.application.corpus.dtos import (
    CollectionDto,
    CollectionPageDto,
    GrantEntryDto,
    GrantPageDto,
)
from rag.application.corpus.mappers import to_collection_dto
from rag.application.corpus.ports import CorpusRepositories, ScopeResolver
from rag.application.shared.ports import TransactionRunner, TxHandle
from rag.domain.corpus.errors import CollectionNotFound


class ListCollections:
    """P-1: the collections in the caller's resolved scope; an empty scope is an empty list."""

    def __init__(
        self, tx: TransactionRunner, repos: CorpusRepositories, scope: ScopeResolver
    ) -> None:
        self._tx, self._r, self._scope = tx, repos, scope

    async def execute(self, query: ListCollectionsQuery) -> CollectionPageDto:
        """One page, ordered by creation."""
        ids = await self._scope.visible_collection_ids(query.identity)

        async def work(tx: TxHandle) -> CollectionPageDto:
            rows = (
                await self._r.collections.list_by_ids(
                    tx, ids, limit=query.limit, offset=query.offset
                )
                if ids
                else []
            )
            return CollectionPageDto(
                [to_collection_dto(c) for c in rows], query.limit, query.offset
            )

        return await self._tx.run_in_tx(work, read_only=True)


class GetCollection:
    """P-2: one collection; absent and ungranted are both 404 (CG-8)."""

    def __init__(
        self, tx: TransactionRunner, repos: CorpusRepositories, scope: ScopeResolver
    ) -> None:
        self._tx, self._r, self._scope = tx, repos, scope

    async def execute(self, query: CollectionQuery) -> CollectionDto:
        """The collection, or ``COLLECTION_NOT_FOUND``."""
        visible = await self._scope.visible_collection_ids(query.identity, only=query.collection_id)
        if query.collection_id not in visible:
            raise CollectionNotFound(f"No granted collection {query.collection_id}.")

        async def work(tx: TxHandle) -> CollectionDto:
            row = await self._r.collections.get(tx, query.collection_id)
            if row is None:
                raise CollectionNotFound(f"No granted collection {query.collection_id}.")
            return to_collection_dto(row)

        return await self._tx.run_in_tx(work, read_only=True)


class ListGrants:
    """P-3: the last recorded overlay answer, for diagnosis. Never authoritative (CG-2, CG-11)."""

    def __init__(
        self,
        tx: TransactionRunner,
        repos: CorpusRepositories,
        scope: ScopeResolver,
        *,
        bound_seconds: int,
    ) -> None:
        self._tx, self._r, self._scope, self._bound = tx, repos, scope, bound_seconds

    async def execute(self, query: ListGrantsQuery) -> GrantPageDto:
        """One page of projection rows plus the freshness of the newest."""
        visible = await self._scope.visible_collection_ids(query.identity, only=query.collection_id)
        if query.collection_id not in visible:
            raise CollectionNotFound(f"No granted collection {query.collection_id}.")

        async def work(tx: TxHandle) -> GrantPageDto:
            if await self._r.collections.get(tx, query.collection_id) is None:
                raise CollectionNotFound(f"No granted collection {query.collection_id}.")
            rows = await self._r.grants.list_active(
                tx, query.collection_id, limit=query.limit, offset=query.offset
            )
            entries = [
                GrantEntryDto(
                    g.subject_type.value,
                    g.subject_id,
                    g.level.value,
                    g.projected_at,
                    g.expires_at,
                    g.source,
                )
                for g in rows
            ]
            as_of = max((g.projected_at for g in rows), default=None)
            return GrantPageDto(entries, query.limit, query.offset, as_of, self._bound)

        return await self._tx.run_in_tx(work, read_only=True)
