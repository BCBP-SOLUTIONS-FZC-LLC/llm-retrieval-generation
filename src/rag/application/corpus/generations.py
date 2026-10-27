"""C-I4 activation (§13.7), C-I6 purge epoch, C-I8 promotion result (§13.5)."""

from __future__ import annotations

from uuid import UUID

from rag.application.corpus.commands import (
    ActivateGenerationCommand,
    MigrationOutcome,
    MigrationResultCommand,
)
from rag.application.corpus.dtos import ActivationDto, MigrationResultDto, PurgeEpochDto
from rag.application.corpus.events import (
    StatusReason,
    collection_status_changed,
    index_generation_activated,
)
from rag.application.corpus.ports import Clock, CorpusRepositories, PointCounter, utc_now
from rag.application.shared.events import EventPublisher
from rag.application.shared.ports import TransactionRunner, TxHandle
from rag.domain.corpus.collection import Collection
from rag.domain.corpus.errors import (
    CollectionFrozen,
    CollectionNotFound,
    CollectionPurging,
    MigrationIncomplete,
    MigrationMismatch,
    OptimisticLockConflict,
)
from rag.domain.shared.enums import CollectionStatus, IndexGenerationStatus


class ActivateIndexGeneration:
    """C-I4: flip the collection to a `verified` generation, atomically (IG-1, IG-5)."""

    def __init__(
        self,
        tx: TransactionRunner,
        repos: CorpusRepositories,
        events: EventPublisher,
        *,
        clock: Clock = utc_now,
    ) -> None:
        self._tx, self._r, self._events, self._clock = tx, repos, events, clock

    async def execute(self, cmd: ActivateGenerationCommand) -> ActivationDto:
        """Idempotent for an already-active generation (LD-37); stale versions conflict."""
        return await self._tx.run_in_tx(lambda tx: self._run(tx, cmd))

    async def _run(self, tx: TxHandle, cmd: ActivateGenerationCommand) -> ActivationDto:
        c = await self._r.collections.get(tx, cmd.collection_id)
        if c is None:
            raise CollectionNotFound(f"No collection {cmd.collection_id}.")
        gen = await self._r.generations.get(tx, c.id, cmd.generation)
        if gen is None:
            raise CollectionNotFound(f"Collection {c.id} has no generation {cmd.generation}.")

        if gen.status is IndexGenerationStatus.ACTIVE:  # a retry after a lost response
            previous = await self._r.generations.latest_retired_before(tx, c.id, gen.generation)
            return ActivationDto(gen.generation, previous, c.record_version)
        if c.status is CollectionStatus.FROZEN:
            raise CollectionFrozen()
        if c.status is CollectionStatus.PURGING:
            raise CollectionPurging()

        now = self._clock()
        activated = gen.activate(now)  # IG-5: GENERATION_NOT_VERIFIED for any other status
        if c.record_version != cmd.record_version:
            raise OptimisticLockConflict(
                "The collection changed; re-resolve and retry.", c.record_version
            )

        # The retire goes first: uq_gen_active admits one active generation (IG-1).
        current = await self._r.generations.get_active(tx, c.id)
        previous = None
        if current is not None:
            await self._r.generations.update(tx, current.retire(now))
            previous = current.generation
        await self._r.generations.update(tx, activated)

        updated = c.with_active_generation(gen.generation)
        if c.status is CollectionStatus.PROVISIONING:  # the first activation makes it retrievable
            updated = updated.activate()
        version = await self._r.collections.update(tx, updated, c.record_version)
        if version is None:
            raise OptimisticLockConflict("The collection changed; re-resolve and retry.")
        await self._events.publish(tx, index_generation_activated(c, activated, previous))
        return ActivationDto(gen.generation, previous, version)


class BumpPurgeEpoch:
    """C-I6: atomic increment, no `record_version` (§5.7.16, LD-98)."""

    def __init__(self, tx: TransactionRunner, repos: CorpusRepositories) -> None:
        self._tx, self._r = tx, repos

    async def execute(self, collection_id: UUID) -> PurgeEpochDto:
        """The new epoch, or ``COLLECTION_NOT_FOUND``."""

        async def work(tx: TxHandle) -> PurgeEpochDto:
            epoch = await self._r.collections.bump_purge_epoch(tx, collection_id)
            if epoch is None:
                raise CollectionNotFound(f"No collection {collection_id}.")
            return PurgeEpochDto(epoch)

        return await self._tx.run_in_tx(work)


class ReportMigrationResult:
    """C-I8: the promotion copy finished or failed; completion is the atomic cutover."""

    def __init__(
        self,
        tx: TransactionRunner,
        repos: CorpusRepositories,
        events: EventPublisher,
        points: PointCounter,
        *,
        clock: Clock = utc_now,
    ) -> None:
        self._tx, self._r, self._events = tx, repos, events
        self._points, self._clock = points, clock

    async def execute(self, cmd: MigrationResultCommand) -> MigrationResultDto:
        """Idempotent on ``migration_id``; ``points_copied`` is checked, not trusted."""
        return await self._tx.run_in_tx(lambda tx: self._run(tx, cmd))

    async def _run(self, tx: TxHandle, cmd: MigrationResultCommand) -> MigrationResultDto:
        target = await self._r.collections.get(tx, cmd.collection_id, lock=True)
        if target is None:
            raise CollectionNotFound(f"No collection {cmd.collection_id}.")
        if not target.is_promotion_target or target.migration_id != cmd.migration_id:
            raise MigrationMismatch("migration_id is not the attempt recorded on the target.")

        done = {
            MigrationOutcome.COMPLETE: CollectionStatus.ACTIVE,
            MigrationOutcome.FAILED: CollectionStatus.MIGRATION_FAILED,
        }[cmd.result]
        if target.status is done:  # a repeat of a result already processed
            return self._body(target)
        if target.status is not CollectionStatus.PROVISIONING:
            raise MigrationMismatch("The target is not in provisioning.")

        if cmd.result is MigrationOutcome.FAILED:
            return await self._fail(tx, target)
        assert cmd.points_copied is not None  # validated at the boundary: 400 otherwise
        return await self._cutover(tx, target, cmd.points_copied)

    async def _fail(self, tx: TxHandle, target: Collection) -> MigrationResultDto:
        failed = target.fail_migration()  # the source stays active and keeps serving
        version = await self._save(tx, failed, target.record_version)
        await self._events.publish(
            tx, collection_status_changed(target, failed, StatusReason.MIGRATION_FAILED)
        )
        return self._body(failed, version)

    async def _cutover(
        self, tx: TxHandle, target: Collection, points_copied: int
    ) -> MigrationResultDto:
        assert target.promoted_from is not None
        source = await self._r.collections.get(tx, target.promoted_from, lock=True)
        if source is None or source.status is not CollectionStatus.ACTIVE:
            raise MigrationMismatch("The source collection is not active.")
        expected = await self._points.count_tender_points(source)
        if points_copied < expected:  # no cutover; Ingestion runs another catch-up copy
            raise MigrationIncomplete(expected, points_copied)

        now = self._clock()
        # One transaction: the source is superseded before the target activates (uq_coll_active).
        superseded = source.supersede()
        await self._save(tx, superseded, source.record_version)
        gen = await self._r.generations.get(tx, target.id, target.active_index_generation)
        if gen is not None and gen.status is IndexGenerationStatus.VERIFIED:
            await self._r.generations.update(tx, gen.activate(now))
        active = target.activate()
        version = await self._save(tx, active, target.record_version)
        for before, after in ((source, superseded), (target, active)):
            await self._events.publish(
                tx, collection_status_changed(before, after, StatusReason.PROMOTION_CUTOVER)
            )
        return self._body(active, version)

    async def _save(self, tx: TxHandle, c: Collection, expected: int) -> int:
        version = await self._r.collections.update(tx, c, expected)
        if version is None:
            raise OptimisticLockConflict("The collection changed; retry.")
        return version

    @staticmethod
    def _body(c: Collection, version: int | None = None) -> MigrationResultDto:
        return MigrationResultDto(
            c.id, c.status.value, c.record_version if version is None else version
        )
