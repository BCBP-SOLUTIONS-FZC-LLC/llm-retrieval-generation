"""The scheduled sweeps of §21.2 (ten of the eleven; see ``infrastructure.sweeps`` for the rest).

All are idempotent, tenant-scoped (none publishes under a system tenant, LD-19) and off the
request path. A per-tenant sweep asks a ``TenantDirectory`` which tenants have work, binds each
tenant with the injected ``scope`` and runs its statements inside that tenant's RLS, so a failure
in one tenant is logged and counted and never stops the others. Four are destructive under
restore skew and refuse to run under ``DR_MODE`` (§18.9, EVT-15); the use cases enforce it
themselves, whatever the runner does.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from contextlib import AbstractContextManager
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Protocol
from uuid import UUID, uuid5

from rag.application.corpus.events import StatusReason, collection_status_changed
from rag.application.corpus.lifecycle import PurgeTenantCorpus
from rag.application.corpus.ports import Clock, CorpusRepositories, utc_now
from rag.application.events.contracts import GenerationFailed, build_event, tender_subject
from rag.application.events.ports import WorkflowStarter
from rag.application.generation.commands import AppendAuditEntry, PurgeArtefacts
from rag.application.shared.events import EventPublisher
from rag.application.shared.ports import TransactionRunner, TxHandle
from rag.domain.generation import audit
from rag.domain.generation.audit import AuditEntryDraft, first_break
from rag.domain.generation.ops import FailedArtefact, StuckArtefact, SweepRepository
from rag.domain.generation.repositories import AuditChainRepository
from rag.domain.shared.enums import AuditActorType, RunFailureReason, RunStage

log = logging.getLogger(__name__)

# Sweep names, as accepted by ``python -m rag.infrastructure.sweeps <name>``.
ORPHAN_ARTEFACTS = "orphan-artefacts"
STALLED_GENERATIONS = "stalled-generations"
IDEMPOTENCY_KEYS = "idempotency-keys"
RETRIEVAL_SETS = "retrieval-sets"
GRAPH_CHECKPOINTS = "graph-checkpoints"
OUTBOX = "outbox"
PROCESSED_EVENTS = "processed-events"
AUDIT_CHAIN = "audit-chain"
TENANT_PURGE = "tenant-purge"
STALLED_PROMOTIONS = "stalled-promotions"


class TenantScopeFactory(Protocol):
    """``bind_tenant_scope`` of the infrastructure: a context manager binding the RLS tenant."""

    def __call__(self, tenant_id: UUID, /) -> AbstractContextManager[None]:
        """Bind ``tenant_id`` for the block."""
        ...


class TenantDirectory(Protocol):
    """Which tenants have candidate rows for a sweep.

    Under FORCE RLS with no ``BYPASSRLS`` role, a tenant list needs a deliberate, read-only
    path: migration 0003's ``rag_sweeper`` role (tenant ids only). The sweep re-checks every row
    under the tenant's own scope, so an over-inclusive answer costs a query and nothing else.
    """

    async def tenants_with_work(self, sweep: str, before: datetime) -> list[UUID]:
        """Tenants with candidates older than ``before`` for ``sweep``."""
        ...


@dataclass(frozen=True, slots=True)
class SweepOutcome:
    """What one run did. ``alerts`` and ``failed_tenants`` make the CronJob exit non-zero."""

    name: str
    tenants: int = 0
    affected: int = 0
    failed_tenants: int = 0
    skipped: str | None = None
    alerts: tuple[str, ...] = field(default_factory=tuple)

    @property
    def ok(self) -> bool:
        """No alert and no tenant failed."""
        return not self.alerts and not self.failed_tenants


class _PerTenantSweep:
    name: str
    destructive: bool = False

    def __init__(
        self,
        *,
        directory: TenantDirectory,
        scope: TenantScopeFactory,
        clock: Clock = utc_now,
        dr_mode: bool = False,
    ) -> None:
        self._directory, self._scope, self._clock, self._dr_mode = directory, scope, clock, dr_mode

    def _cutoff(self) -> datetime:
        raise NotImplementedError

    def _unavailable(self) -> str | None:
        return None

    async def _tenant(self, tenant_id: UUID) -> int:
        raise NotImplementedError

    async def run(self) -> SweepOutcome:
        """Run once over every tenant with work."""
        if self.destructive and self._dr_mode:
            return SweepOutcome(self.name, skipped="DR_MODE disables this sweep (§18.9)")
        if (reason := self._unavailable()) is not None:
            return SweepOutcome(self.name, skipped=reason)
        tenants = await self._directory.tenants_with_work(self.name, self._cutoff())
        affected = failed = 0
        for tenant_id in tenants:
            try:
                with self._scope(tenant_id):
                    affected += await self._tenant(tenant_id)
            except Exception:  # noqa: BLE001 - one tenant must not stop the sweep
                failed += 1
                log.exception("sweep %s failed for tenant %s", self.name, tenant_id)
        return SweepOutcome(self.name, len(tenants), affected, failed)


async def _fail_and_announce(
    tx: TxHandle,
    repo: SweepRepository,
    events: EventPublisher,
    audit_append: AppendAuditEntry,
    service_account_id: UUID,
    stuck: StuckArtefact,
    now: datetime,
) -> bool:
    """Close the artefact and its run as ``failed`` / ``internal``, publish and audit it."""
    failed: FailedArtefact | None = await repo.fail_artefact(tx, stuck.artefact_id, now)
    if failed is None:
        return False
    payload = GenerationFailed(
        artefact_id=str(failed.artefact_id),
        generation_id=None if failed.run_id is None else str(failed.run_id),
        tender_id=None if failed.tender_id is None else str(failed.tender_id),
        principal_id=str(failed.principal_id),
        reason=RunFailureReason.INTERNAL,
        failed_at_stage=(
            None if failed.failed_at_stage is None else RunStage(failed.failed_at_stage)
        ),
        sections_complete=failed.sections_complete,
        sections_total=failed.sections_total,
        sections_durable=failed.sections_complete > 0,
    )
    tenant_id = failed.tenant_id
    await events.publish(tx, build_event(tenant_id, tender_subject(failed.tender_id), payload))
    await audit_append(
        tx,
        AuditEntryDraft(
            tenant_id=tenant_id,
            occurred_at=now,
            entry_type=audit.GENERATION_FAILED[0],
            action=audit.GENERATION_FAILED[1],
            actor_type=AuditActorType.SERVICE_ACCOUNT,
            actor_id=service_account_id,
            artefact_id=failed.artefact_id,
            target_type="artefact",
            target_id=str(failed.artefact_id),
            source_event_type=audit.GENERATION_FAILED[0],
            source_event_id=f"swept:{failed.artefact_id}:{failed.run_id}",
            metadata={
                "generation_run_id": None if failed.run_id is None else str(failed.run_id),
                "failure_reason": RunFailureReason.INTERNAL.value,
                "failed_at_stage": failed.failed_at_stage,
            },
        ),
    )
    return True


class OrphanPendingArtefacts(_PerTenantSweep):
    """``pending`` artefacts past ``ORPHAN_PENDING_SECONDS``: re-attempt the start once (ASYNC-2).

    A ``pending`` row is a workflow that failed to start after its shell committed. The start is
    made under the run's ``workflow_id`` from ``run_input``, which Temporal absorbs if the workflow
    in fact started (RUN-8); a second failure moves artefact and run to ``failed`` / ``internal``
    and publishes ``GenerationFailed``. Rows are never deleted. With no workflow client wired the
    sweep does nothing: failing rows it could not retry would be a false verdict.
    """

    name = ORPHAN_ARTEFACTS
    destructive = True

    def __init__(
        self,
        tx: TransactionRunner,
        repo: SweepRepository,
        starter: WorkflowStarter,
        events: EventPublisher,
        audit_append: AppendAuditEntry,
        service_account_id: UUID,
        *,
        orphan_seconds: int,
        **kw: object,
    ) -> None:
        super().__init__(**kw)  # type: ignore[arg-type]
        self._tx, self._repo, self._starter, self._events = tx, repo, starter, events
        self._audit, self._sa, self._seconds = audit_append, service_account_id, orphan_seconds

    def _cutoff(self) -> datetime:
        return self._clock() - timedelta(seconds=self._seconds)

    def _unavailable(self) -> str | None:
        return None if self._starter.available else "no workflow client is wired"

    async def _tenant(self, tenant_id: UUID) -> int:
        stuck = await self._tx.run_in_tx(
            lambda tx: self._repo.pending_artefacts(tx, self._cutoff()), read_only=True
        )
        failed = 0
        for item in stuck:
            try:
                await self._starter.start(item.workflow_id, item.run_input)
                continue
            except Exception:  # noqa: BLE001 - the second failure is the verdict
                log.warning("restart of artefact %s failed", item.artefact_id, exc_info=True)
            failed += await self._fail(item)
        return failed

    async def _fail(self, item: StuckArtefact) -> int:
        now = self._clock()

        async def work(tx: TxHandle) -> bool:
            return await _fail_and_announce(
                tx, self._repo, self._events, self._audit, self._sa, item, now
            )

        return int(await self._tx.run_in_tx(work))


class StalledGeneratingArtefacts(_PerTenantSweep):
    """``generating`` artefacts past ``STALLED_GENERATING_SECONDS`` with no live run -> ``failed``.

    The gap a crash between §7.5's two writes leaves: a synchronous run has no workflow to recover
    it. A cancelled artefact is ``cancelled``, so this cannot reach one (ART-6).
    """

    name = STALLED_GENERATIONS
    destructive = True

    def __init__(
        self,
        tx: TransactionRunner,
        repo: SweepRepository,
        events: EventPublisher,
        audit_append: AppendAuditEntry,
        service_account_id: UUID,
        *,
        stalled_seconds: int,
        **kw: object,
    ) -> None:
        super().__init__(**kw)  # type: ignore[arg-type]
        self._tx, self._repo, self._events = tx, repo, events
        self._audit, self._sa, self._seconds = audit_append, service_account_id, stalled_seconds

    def _cutoff(self) -> datetime:
        return self._clock() - timedelta(seconds=self._seconds)

    async def _tenant(self, tenant_id: UUID) -> int:
        async def work(tx: TxHandle) -> int:
            now, failed = self._clock(), 0
            for item in await self._repo.stalled_artefacts(tx, self._cutoff()):
                failed += await _fail_and_announce(
                    tx, self._repo, self._events, self._audit, self._sa, item, now
                )
            return failed

        return await self._tx.run_in_tx(work)


class ClearIdempotencyKeys(_PerTenantSweep):
    """Null ``generation_runs.idempotency_key`` past ``IDEMPOTENCY_TTL_SECONDS`` (RUN-3)."""

    name = IDEMPOTENCY_KEYS

    def __init__(
        self, tx: TransactionRunner, repo: SweepRepository, *, ttl_seconds: int, **kw: object
    ) -> None:
        super().__init__(**kw)  # type: ignore[arg-type]
        self._tx, self._repo, self._ttl = tx, repo, ttl_seconds

    def _cutoff(self) -> datetime:
        return self._clock() - timedelta(seconds=self._ttl)

    async def _tenant(self, tenant_id: UUID) -> int:
        return await self._tx.run_in_tx(
            lambda tx: self._repo.clear_idempotency_keys(tx, self._cutoff())
        )


class ReclaimOrphanRetrievalSets(_PerTenantSweep):
    """Delete sets with no artefact or section version past the age floor (ART-9, API-9)."""

    name = RETRIEVAL_SETS
    destructive = True
    BATCH = 500

    def __init__(
        self, tx: TransactionRunner, repo: SweepRepository, *, min_age_days: int, **kw: object
    ) -> None:
        super().__init__(**kw)  # type: ignore[arg-type]
        self._tx, self._repo, self._days = tx, repo, min_age_days

    def _cutoff(self) -> datetime:
        return self._clock() - timedelta(days=self._days)

    async def _tenant(self, tenant_id: UUID) -> int:
        total = 0
        while True:  # batched so one transaction never holds a tenant's whole backlog
            n = await self._tx.run_in_tx(
                lambda tx: self._repo.delete_orphan_retrieval_sets(tx, self._cutoff(), self.BATCH)
            )
            total += n
            if n < self.BATCH:
                return total


class SweepGraphCheckpoints(_PerTenantSweep):
    """Delete checkpoints left by failed activities (GC-1); the normal path sweeps on completion."""

    name = GRAPH_CHECKPOINTS

    def __init__(
        self, tx: TransactionRunner, repo: SweepRepository, *, max_age_hours: int, **kw: object
    ) -> None:
        super().__init__(**kw)  # type: ignore[arg-type]
        self._tx, self._repo, self._hours = tx, repo, max_age_hours

    def _cutoff(self) -> datetime:
        return self._clock() - timedelta(hours=self._hours)

    async def _tenant(self, tenant_id: UUID) -> int:
        return await self._tx.run_in_tx(
            lambda tx: self._repo.delete_stale_checkpoints(tx, self._cutoff())
        )


class VerifyAuditChains(_PerTenantSweep):
    """Walk each tenant's chain, recompute both hashes, alert on the first break (AC-2, AC-3).

    A break is a tamper signal: it is reported, never repaired. The runner exits non-zero so the
    CronJob's failure pages. ponytail: reads a whole chain per tenant; page by ``after_seq`` with a
    stored checkpoint if chains outgrow memory.
    """

    name = AUDIT_CHAIN

    def __init__(self, tx: TransactionRunner, chain: AuditChainRepository, **kw: object) -> None:
        super().__init__(**kw)  # type: ignore[arg-type]
        self._tx, self._chain = tx, chain
        self._breaks: list[str] = []

    def _cutoff(self) -> datetime:
        return self._clock()

    async def _tenant(self, tenant_id: UUID) -> int:
        entries = await self._tx.run_in_tx(
            lambda tx: self._chain.list_chain(tx, tenant_id), read_only=True
        )
        seq = first_break(entries)
        if seq is not None:
            message = f"audit chain of tenant {tenant_id} breaks at seq {seq}"
            log.error("ALERT %s", message)
            self._breaks.append(message)
            return 1
        return 0

    async def run(self) -> SweepOutcome:
        """Run once; every break is an alert."""
        self._breaks.clear()
        out = await super().run()
        return SweepOutcome(
            out.name,
            out.tenants,
            out.affected,
            out.failed_tenants,
            out.skipped,
            tuple(self._breaks),
        )


class PurgeDueTenants(_PerTenantSweep):
    """Phase two of a tenant purge, ``TENANT_PURGE_SOAK_DAYS`` after the ``purged`` row (EVT-15).

    Per tenant: the artefacts and their S3 objects first (``llm_generation``, the chain is kept,
    AC-6), then the collection rows (``llm_corpus``, the lifecycle row stays, TL-1). A failure in
    the first leaves the lifecycle row due, so the next run retries both. Valkey entries and
    evaluation samples (§21.1) are not reachable from here yet. ponytail: a tenant stays "due"
    forever (the row is permanent), so each run repeats a cheap no-op per purged tenant; add
    ``purge_completed_at`` when that count matters.
    """

    name = TENANT_PURGE
    destructive = True

    def __init__(
        self,
        repos: CorpusRepositories,
        corpus_tx: TransactionRunner,
        purge_artefacts: PurgeArtefacts,
        purge_corpus: PurgeTenantCorpus,
        service_account_id: UUID,
        *,
        soak_days: int,
        **kw: object,
    ) -> None:
        super().__init__(**kw)  # type: ignore[arg-type]
        self._repos, self._tx = repos, corpus_tx
        self._artefacts, self._corpus, self._sa, self._soak = (
            purge_artefacts,
            purge_corpus,
            service_account_id,
            soak_days,
        )

    def _cutoff(self) -> datetime:
        return self._clock() - timedelta(days=self._soak)

    async def _tenant(self, tenant_id: UUID) -> int:
        row = await self._tx.run_in_tx(
            lambda tx: self._repos.lifecycle.get(tx, tenant_id), read_only=True
        )
        if row is None or not row.soak_elapsed(self._clock(), self._soak):
            return 0
        await self._artefacts.execute(
            tenant_id=tenant_id,
            tender_id=None,
            event_id=uuid5(tenant_id, "tenant-purge"),
            service_account_id=self._sa,
            reason="tenant_purged",
        )
        return int(await self._corpus.execute(tenant_id))


class StalledPromotions(_PerTenantSweep):
    """Promotion targets stuck ``provisioning`` past the timeout -> ``migration_failed``.

    The realistic failure is Ingestion never calling C-I8, which nothing else reaches. Moves the
    target, publishes ``CollectionStatusChanged`` and alerts; the source keeps serving.
    """

    name = STALLED_PROMOTIONS

    def __init__(
        self,
        tx: TransactionRunner,
        repos: CorpusRepositories,
        events: EventPublisher,
        *,
        timeout_seconds: int,
        **kw: object,
    ) -> None:
        super().__init__(**kw)  # type: ignore[arg-type]
        self._tx, self._repos, self._events, self._seconds = tx, repos, events, timeout_seconds

    def _cutoff(self) -> datetime:
        return self._clock() - timedelta(seconds=self._seconds)

    async def _tenant(self, tenant_id: UUID) -> int:
        async def work(tx: TxHandle) -> int:
            moved = 0
            for c in await self._repos.collections.list_stalled_promotions(tx, self._cutoff()):
                failed = c.fail_migration()
                if await self._repos.collections.update(tx, failed, c.record_version) is None:
                    continue  # changed under us (C-I8 arrived): leave it
                await self._events.publish(
                    tx, collection_status_changed(c, failed, StatusReason.MIGRATION_FAILED)
                )
                log.error("ALERT promotion of collection %s timed out", c.id)
                moved += 1
            return moved

        return await self._tx.run_in_tx(work)


class OutboxPruner(Protocol):
    """``eventcommon.outbox.PostgresStore`` / ``Runner.prune_published``."""

    async def prune_published(self, older_than: timedelta, limit: int) -> int:
        """Delete up to ``limit`` published rows older than ``older_than``."""
        ...


class LedgerPruner(Protocol):
    """``eventcommon.inbox.Store`` for one consumer name."""

    async def prune(self, retention: timedelta, batch: int = ...) -> int:
        """Delete this consumer's entries older than ``retention``."""
        ...


class PrunePublishedOutbox:
    """Delete published ``outbox_events`` rows older than the window, in both databases (OBX-6).

    Dead letters are never pruned: a discarded dead letter is an event nobody received (OBX-5).
    """

    name = OUTBOX
    destructive = False
    BATCH = 1000

    def __init__(self, stores: Mapping[str, OutboxPruner], *, older_than_days: int) -> None:
        self._stores, self._older = stores, timedelta(days=older_than_days)

    async def run(self) -> SweepOutcome:
        """Prune each database's table."""
        total = 0
        for store in self._stores.values():
            while (n := await store.prune_published(self._older, self.BATCH)) > 0:
                total += n
                if n < self.BATCH:
                    break
        return SweepOutcome(self.name, tenants=0, affected=total)


class PruneProcessedEvents:
    """Prune ``processed_events`` per consumer: eight days, longer for destructive ones (EVT-16)."""

    name = PROCESSED_EVENTS
    destructive = False

    def __init__(
        self,
        ledgers: Mapping[str, LedgerPruner],
        destructive: frozenset[str],
        *,
        ordinary_days: int,
        destructive_days: int,
    ) -> None:
        self._ledgers, self._destructive = ledgers, destructive
        self._ordinary, self._long = timedelta(days=ordinary_days), timedelta(days=destructive_days)

    async def run(self) -> SweepOutcome:
        """One batched delete per consumer name."""
        total = 0
        for name, ledger in self._ledgers.items():
            total += await ledger.prune(self._long if name in self._destructive else self._ordinary)
        return SweepOutcome(self.name, tenants=0, affected=total)
