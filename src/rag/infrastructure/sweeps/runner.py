"""Builds and runs one sweep (§21.2, §18.8).

**Deviation from §18.8**, recorded in the plan: the LLD runs the sweeps as a leader-elected worker
inside the service deployment; this is the reconciler shape of the Go reference (one invocation,
one sweep, run by a Kubernetes CronJob). The guard §18.8 asks for is kept: each run takes a
Postgres advisory lock for its duration, on the instance it targets, and **skips** when it cannot
(an overlapping run), so a slow run is never doubled. The lock is ``pg_try_advisory_xact_lock`` in a
transaction held for the sweep, because session locks do not survive PgBouncer's transaction mode.

Sweeps use their own small pools, never the request pools (§18.8), and the enumeration pools log
in as a member of ``rag_sweeper`` (``directory``). Of §21.2's eleven, ten are here. The zombie-run
sweep needs Temporal's liveness and the orphan-rendered-objects sweep needs an S3 listing; neither
dependency exists yet, so they are not faked.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from eventcommon.inbox import Store
from eventcommon.outbox import PostgresStore
from fastapicommon.logging import get_logger
from pgcommon import Pool, run_in_tx

from rag.application.corpus.lifecycle import PurgeTenantCorpus
from rag.application.corpus.ports import CorpusRepositories
from rag.application.events import contracts as c
from rag.application.generation.clock import SystemClock
from rag.application.generation.commands import AppendAuditEntry, PurgeArtefacts
from rag.application.shared.ports import TransactionRunner
from rag.application.sweeps import sweeps as s
from rag.infrastructure.config import Settings
from rag.infrastructure.events.publisher import OutboxEventPublisher
from rag.infrastructure.persistence.corpus.repositories import (
    PgCollectionRepository,
    PgGrantRepository,
    PgIndexGenerationRepository,
    PgRetentionRepository,
    PgTenantLifecycleRepository,
)
from rag.infrastructure.persistence.generation.ops import PgSweepRepository
from rag.infrastructure.persistence.generation.repositories import (
    PgArtefactPurgeRepository,
    PgAuditChainRepository,
)
from rag.infrastructure.persistence.pools import bind_tenant_scope, pool_config
from rag.infrastructure.persistence.transactions import PgTransactionRunner
from rag.infrastructure.storage.artefact_content_store import S3ArtefactContentStore
from rag.infrastructure.sweeps.directory import PgTenantDirectory
from rag.infrastructure.temporal.factory import build_orphan_restarter

log = get_logger(__name__)

CORPUS_SWEEPS = frozenset({s.TENANT_PURGE, s.STALLED_PROMOTIONS})
NAMES = (
    s.ORPHAN_ARTEFACTS,
    s.STALLED_GENERATIONS,
    s.IDEMPOTENCY_KEYS,
    s.RETRIEVAL_SETS,
    s.GRAPH_CHECKPOINTS,
    s.OUTBOX,
    s.PROCESSED_EVENTS,
    s.AUDIT_CHAIN,
    s.TENANT_PURGE,
    s.STALLED_PROMOTIONS,
)


@dataclass
class Pools:
    """Four small pools: work and enumeration, per instance."""

    corpus: Pool
    generation: Pool
    corpus_enum: Pool
    generation_enum: Pool

    async def close(self) -> None:
        """Drain and close all four."""
        for pool in (self.corpus, self.generation, self.corpus_enum, self.generation_enum):
            await pool.drain_and_close()


async def open_pools(settings: Settings) -> Pools:
    """Open the sweep pools.

    The enumeration DSNs are required: without them a tenant list would silently come back empty
    under RLS, and a sweep that finds nothing looks like a healthy one.
    """
    if not (settings.corpus_sweep_database_url and settings.generation_sweep_database_url):
        raise RuntimeError(
            "CORPUS_SWEEP_DATABASE_URL and GENERATION_SWEEP_DATABASE_URL are required "
            "(login roles that are members of rag_sweeper, migration 0003)"
        )
    if settings.sweep_pool_max_conns < 2:
        raise RuntimeError("SWEEP_POOL_MAX_CONNS must be at least 2 (one holds the leader lock)")

    async def pool(dsn: str, name: str, guc: bool) -> Pool:
        return await Pool.create(
            pool_config(dsn, name, settings, max_conns=settings.sweep_pool_max_conns, guc=guc)
        )

    return Pools(
        await pool(settings.corpus_database_url, "sweep-corpus", True),
        await pool(settings.generation_database_url, "sweep-generation", True),
        await pool(settings.corpus_sweep_database_url, "sweep-corpus-enum", False),
        await pool(settings.generation_sweep_database_url, "sweep-generation-enum", False),
    )


def lock_key(name: str) -> int:
    """A stable signed 64-bit advisory-lock key per sweep name."""
    return int.from_bytes(
        hashlib.sha256(f"rag-sweep:{name}".encode()).digest()[:8], "big", signed=True
    )


def build_sweep(settings: Settings, pools: Pools, name: str) -> Any:
    """The use case for ``name`` wired to the sweep pools."""
    corpus_tx: TransactionRunner = PgTransactionRunner(pools.corpus)
    generation_tx: TransactionRunner = PgTransactionRunner(pools.generation)
    directory = PgTenantDirectory(pools.corpus_enum, pools.generation_enum)
    repos = CorpusRepositories(
        collections=PgCollectionRepository(),
        generations=PgIndexGenerationRepository(),
        grants=PgGrantRepository(),
        retention=PgRetentionRepository(),
        lifecycle=PgTenantLifecycleRepository(),
    )
    account = UUID(settings.service_account_id)
    audit = AppendAuditEntry(PgAuditChainRepository())
    events = OutboxEventPublisher()
    common: dict[str, Any] = {
        "directory": directory,
        "scope": bind_tenant_scope,
        "dr_mode": settings.dr_mode,
    }
    sweeps = PgSweepRepository()
    factories: dict[str, Callable[[], Any]] = {
        s.ORPHAN_ARTEFACTS: lambda: s.OrphanPendingArtefacts(
            generation_tx,
            sweeps,
            build_orphan_restarter(settings),
            events,
            audit,
            account,
            orphan_seconds=settings.orphan_pending_seconds,
            **common,
        ),
        s.STALLED_GENERATIONS: lambda: s.StalledGeneratingArtefacts(
            generation_tx,
            sweeps,
            events,
            audit,
            account,
            stalled_seconds=settings.stalled_generating_seconds,
            **common,
        ),
        s.IDEMPOTENCY_KEYS: lambda: s.ClearIdempotencyKeys(
            generation_tx, sweeps, ttl_seconds=settings.idempotency_ttl_seconds, **common
        ),
        s.RETRIEVAL_SETS: lambda: s.ReclaimOrphanRetrievalSets(
            generation_tx, sweeps, min_age_days=settings.retrieval_set_orphan_min_age_days, **common
        ),
        s.GRAPH_CHECKPOINTS: lambda: s.SweepGraphCheckpoints(
            generation_tx, sweeps, max_age_hours=settings.graph_checkpoint_max_age_hours, **common
        ),
        s.AUDIT_CHAIN: lambda: s.VerifyAuditChains(
            generation_tx, PgAuditChainRepository(), **common
        ),
        s.TENANT_PURGE: lambda: s.PurgeDueTenants(
            repos,
            corpus_tx,
            PurgeArtefacts(
                generation_tx,
                PgArtefactPurgeRepository(),
                audit,
                S3ArtefactContentStore(
                    settings.s3_bucket,
                    region=settings.s3_region,
                    endpoint_url=settings.s3_endpoint_url,
                    kms_key_id=settings.s3_kms_key_id,
                ),
                SystemClock(),
            ),
            PurgeTenantCorpus(
                corpus_tx,
                repos,
                soak_days=settings.tenant_purge_soak_days,
                dr_mode=settings.dr_mode,
            ),
            account,
            soak_days=settings.tenant_purge_soak_days,
            **common,
        ),
        s.STALLED_PROMOTIONS: lambda: s.StalledPromotions(
            corpus_tx, repos, events, timeout_seconds=settings.promotion_timeout_seconds, **common
        ),
        s.OUTBOX: lambda: s.PrunePublishedOutbox(
            {"corpus": PostgresStore(pools.corpus), "generation": PostgresStore(pools.generation)},
            older_than_days=settings.outbox_prune_older_than_days,
        ),
        s.PROCESSED_EVENTS: lambda: s.PruneProcessedEvents(
            {
                n: Store(pools.generation, n)
                for n in (
                    c.CONSUMER_PURGE_PROJECTION,
                    c.CONSUMER_PACK_INVALIDATION,
                    c.CONSUMER_MEMBERSHIP,
                    c.CONSUMER_TENDER_LIFECYCLE,
                    c.CONSUMER_QUOTA_RESUME,
                )
            },
            c.DESTRUCTIVE_CONSUMERS,
            ordinary_days=settings.processed_events_retention_days,
            destructive_days=settings.destructive_ledger_retention_days,
        ),
    }
    return factories[name]()


async def run_exclusive(pool: Pool, name: str, sweep: Any) -> s.SweepOutcome:
    """Run ``sweep`` under the advisory lock of its instance, or skip when it is held."""

    async def locked(conn: Any) -> s.SweepOutcome:
        if not await conn.fetchval("SELECT pg_try_advisory_xact_lock($1)", lock_key(name)):
            return s.SweepOutcome(name, skipped="another run holds the lock")
        outcome: s.SweepOutcome = await sweep.run()
        return outcome

    return await run_in_tx(pool, locked)


async def run_sweep(settings: Settings, name: str, pools: Pools | None = None) -> s.SweepOutcome:
    """Run one sweep by name; ``pools`` lets a caller share pools across sweeps."""
    if name not in NAMES:
        raise ValueError(f"unknown sweep {name!r}; one of {', '.join(NAMES)}")
    own = pools is None
    pools = pools or await open_pools(settings)
    try:
        sweep = build_sweep(settings, pools, name)
        pool = pools.corpus if name in CORPUS_SWEEPS else pools.generation
        outcome = await run_exclusive(pool, name, sweep)
        log.info("sweep_finished", sweep=name, outcome=str(outcome))
        return outcome
    finally:
        if own:
            await pools.close()
