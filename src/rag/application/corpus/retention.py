"""C-I5 (retention read for the sweep) and P-6 (set the policy, LD-76)."""

from __future__ import annotations

from uuid import UUID

from rag.application.corpus.commands import PutRetentionCommand
from rag.application.corpus.dtos import PutRetentionResultDto, RetentionDto
from rag.application.corpus.ports import CorpusRepositories, ScopeResolver
from rag.application.shared.ports import TransactionRunner, TxHandle
from rag.domain.corpus.errors import CollectionNotFound, OptimisticLockConflict
from rag.domain.corpus.retention import RetentionPolicy


def _dto(p: RetentionPolicy) -> RetentionDto:
    return RetentionDto(
        p.collection_id, p.retain_days, p.legal_hold, p.record_version, p.updated_at
    )


class GetRetention:
    """C-I5: the policy the sweep applies (no v1 caller)."""

    def __init__(self, tx: TransactionRunner, repos: CorpusRepositories) -> None:
        self._tx, self._r = tx, repos

    async def execute(self, collection_id: UUID) -> RetentionDto:
        """404 for an unknown collection. Also 404 (distinct detail) before any P-6 write.

        Deviation: the LLD gives no status for a known collection without a policy.
        """

        async def work(tx: TxHandle) -> RetentionDto:
            if await self._r.collections.get(tx, collection_id) is None:
                raise CollectionNotFound(f"No collection {collection_id}.")
            policy = await self._r.retention.get(tx, collection_id)
            if policy is None:
                raise CollectionNotFound(f"Collection {collection_id} has no retention policy.")
            return _dto(policy)

        return await self._tx.run_in_tx(work, read_only=True)


class PutRetention:
    """P-6: first write (no version) creates and returns 201; a versioned write updates."""

    def __init__(
        self, tx: TransactionRunner, repos: CorpusRepositories, scope: ScopeResolver
    ) -> None:
        self._tx, self._r, self._scope = tx, repos, scope

    async def execute(self, cmd: PutRetentionCommand) -> PutRetentionResultDto:
        """Every mismatch is ``OPTIMISTIC_LOCK_CONFLICT``, carrying the version when one exists."""
        identity = cmd.identity
        assert identity.principal_id is not None  # a public route: authenticated
        principal: UUID = identity.principal_id
        visible = await self._scope.visible_collection_ids(identity, only=cmd.collection_id)
        if cmd.collection_id not in visible:
            raise CollectionNotFound(f"No granted collection {cmd.collection_id}.")

        async def work(tx: TxHandle) -> PutRetentionResultDto:
            collection = await self._r.collections.get(tx, cmd.collection_id)
            if collection is None:
                raise CollectionNotFound(f"No granted collection {cmd.collection_id}.")
            current = await self._r.retention.get(tx, cmd.collection_id, lock=True)

            if cmd.record_version is None:
                if current is not None:
                    raise OptimisticLockConflict(
                        "A retention policy exists; send its record_version.",
                        current.record_version,
                    )
                created = await self._r.retention.insert(
                    tx,
                    RetentionPolicy(
                        cmd.collection_id,
                        collection.tenant_id,
                        cmd.retain_days,
                        cmd.legal_hold,
                        principal,
                    ),
                )
                if created is None:  # lost the first-write race
                    raise OptimisticLockConflict("A retention policy was just created; retry.")
                return PutRetentionResultDto(_dto(created), created=True)

            if current is None:
                raise OptimisticLockConflict("No retention policy exists; retry without a version.")
            changed = current.updated(
                retain_days=cmd.retain_days,
                legal_hold=cmd.legal_hold,
                updated_by=principal,
                expected_version=cmd.record_version,
            )
            stored = await self._r.retention.update(tx, changed, cmd.record_version)
            if stored is None:
                raise OptimisticLockConflict(
                    "The retention policy changed; retry.", current.record_version
                )
            return PutRetentionResultDto(_dto(stored), created=False)

        return await self._tx.run_in_tx(work)
