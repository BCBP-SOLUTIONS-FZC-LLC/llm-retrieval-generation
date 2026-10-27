"""The §21.2 sweeps over in-memory fakes: what each acts on, what DR_MODE refuses, isolation."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest
from tests.support_corpus import NOW, TENANT, TENDER, World
from tests.support_events import SERVICE_ACCOUNT, ListEvents
from tests.support_generation import (
    FakeAuditRepo,
    FakePurgeRepo,
    FakeStore,
    FakeTx,
    FixedClock,
    make_artefact,
)

from rag.application.corpus.lifecycle import PurgeTenantCorpus
from rag.application.generation.commands import AppendAuditEntry, PurgeArtefacts
from rag.application.sweeps import sweeps as s
from rag.domain.corpus.tenant_lifecycle import TenantLifecycle, TenantState
from rag.domain.generation import audit
from rag.domain.generation.audit import AuditEntry, AuditEntryDraft
from rag.domain.generation.ops import FailedArtefact, StuckArtefact
from rag.domain.shared.enums import ArtefactKind, AuditActorType, CollectionStatus
from rag.infrastructure.events.schemas import validate_published

OTHER = UUID("0a3f0000-0000-7000-8000-000000000009")
LATER = NOW + timedelta(days=30)


class Scope:
    """``bind_tenant_scope`` recording which tenant is bound while work runs."""

    def __init__(self) -> None:
        self.current: UUID | None = None
        self.bound: list[UUID] = []

    @contextmanager
    def __call__(self, tenant_id: UUID) -> Iterator[None]:
        self.current = tenant_id
        self.bound.append(tenant_id)
        try:
            yield
        finally:
            self.current = None


class Directory:
    """``TenantDirectory`` with a fixed answer; remembers the cutoff it was asked about."""

    def __init__(self, *tenants: UUID) -> None:
        self.tenants, self.asked = list(tenants), []

    async def tenants_with_work(self, sweep: str, before: datetime) -> list[UUID]:
        self.asked.append((sweep, before))
        return self.tenants


class Repo:
    """``SweepRepository``: per-tenant candidates, and what was done to them."""

    def __init__(self, scope: Scope) -> None:
        self.scope = scope
        self.pending: dict[UUID, list[StuckArtefact]] = {}
        self.stalled: dict[UUID, list[StuckArtefact]] = {}
        self.failed: list[UUID] = []
        self.cleared: list[tuple[UUID | None, datetime]] = []
        self.set_batches: list[int] = []
        self.checkpoints: list[datetime] = []
        self.boom: set[UUID] = set()

    def _guard(self) -> None:
        if self.scope.current in self.boom:
            raise RuntimeError("tenant store down")

    async def pending_artefacts(self, tx: Any, before: datetime) -> list[StuckArtefact]:
        self._guard()
        return self.pending.get(self.scope.current, [])  # type: ignore[arg-type]

    async def stalled_artefacts(self, tx: Any, before: datetime) -> list[StuckArtefact]:
        self._guard()
        return self.stalled.get(self.scope.current, [])  # type: ignore[arg-type]

    async def fail_artefact(
        self, tx: Any, artefact_id: UUID, now: datetime
    ) -> FailedArtefact | None:
        self.failed.append(artefact_id)
        assert self.scope.current is not None
        return FailedArtefact(
            artefact_id, self.scope.current, TENDER, uuid4(), uuid4(), "stage2", 2, 5
        )

    async def clear_idempotency_keys(self, tx: Any, before: datetime) -> int:
        self._guard()
        self.cleared.append((self.scope.current, before))
        return 3

    async def delete_orphan_retrieval_sets(self, tx: Any, before: datetime, limit: int) -> int:
        n = self.set_batches.pop(0)
        return n

    async def delete_stale_checkpoints(self, tx: Any, before: datetime) -> int:
        self._guard()
        self.checkpoints.append(before)
        return 1


class Starter:
    def __init__(self, available: bool = True, fail: bool = False) -> None:
        self.available, self.fail = available, fail
        self.started: list[str | None] = []

    async def start(self, workflow_id: str | None, run_input: Any) -> None:
        self.started.append(workflow_id)
        if self.fail:
            raise ConnectionError("temporal down")


def stuck(tender: UUID | None = TENDER) -> StuckArtefact:
    return StuckArtefact(uuid4(), tender, uuid4(), uuid4(), f"wf-{uuid4()}", {"k": "v"})


def kit(*tenants: UUID, dr_mode: bool = False) -> dict[str, Any]:
    scope = Scope()
    return {
        "scope": scope,
        "directory": Directory(*tenants),
        "clock": lambda: NOW,
        "dr_mode": dr_mode,
    }


def common(k: dict[str, Any]) -> dict[str, Any]:
    return {x: k[x] for x in ("scope", "directory", "clock", "dr_mode")}


# --- the four destructive-under-skew sweeps and the tenant purge refuse under DR_MODE ----------
def destructive(dr: bool) -> list[Any]:
    k = kit(TENANT, dr_mode=dr)
    repo, events, chain = Repo(k["scope"]), ListEvents(), AppendAuditEntry(FakeAuditRepo())
    w = World()
    purge = PurgeArtefacts(FakeTx(), FakePurgeRepo([]), chain, FakeStore(), FixedClock())
    return [
        s.OrphanPendingArtefacts(
            FakeTx(),
            repo,
            Starter(),
            events,
            chain,
            SERVICE_ACCOUNT,
            orphan_seconds=300,
            **common(k),
        ),
        s.StalledGeneratingArtefacts(
            FakeTx(), repo, events, chain, SERVICE_ACCOUNT, stalled_seconds=900, **common(k)
        ),
        s.ReclaimOrphanRetrievalSets(FakeTx(), repo, min_age_days=30, **common(k)),
        s.PurgeDueTenants(
            w.repos,
            w.tx,
            purge,
            PurgeTenantCorpus(w.tx, w.repos, soak_days=7),
            SERVICE_ACCOUNT,
            soak_days=7,
            **common(k),
        ),
    ]


@pytest.mark.parametrize("index", range(4))
async def test_dr_mode_hard_disables_the_destructive_sweeps(index: int) -> None:
    """§18.9: nothing is asked of the directory and nothing is touched."""
    sweep = destructive(True)[index]
    out = await sweep.run()
    assert out.skipped and "DR_MODE" in out.skipped and out.affected == 0
    assert sweep._directory.asked == []


async def test_non_destructive_sweeps_still_run_under_dr_mode() -> None:
    k = kit(TENANT, dr_mode=True)
    repo = Repo(k["scope"])
    out = await s.ClearIdempotencyKeys(FakeTx(), repo, ttl_seconds=3600, **common(k)).run()
    assert out.skipped is None and out.affected == 3


# --- orphan artefacts ---------------------------------------------------------------------------
def orphan(
    starter: Starter, repo: Repo, k: dict[str, Any]
) -> tuple[s.OrphanPendingArtefacts, ListEvents, FakeAuditRepo]:
    events, chain = ListEvents(), FakeAuditRepo()
    sweep = s.OrphanPendingArtefacts(
        FakeTx(),
        repo,
        starter,
        events,
        AppendAuditEntry(chain),
        SERVICE_ACCOUNT,
        orphan_seconds=300,
        **common(k),
    )
    return sweep, events, chain


async def test_orphan_sweep_does_nothing_without_a_workflow_client() -> None:
    """A verdict on rows it could not retry would be false: skipped, directory never asked."""
    k = kit(TENANT)
    repo = Repo(k["scope"])
    repo.pending[TENANT] = [stuck()]
    sweep, events, _ = orphan(Starter(available=False), repo, k)
    out = await sweep.run()
    assert out.skipped and "workflow client" in out.skipped
    assert repo.failed == [] and events.events == [] and k["directory"].asked == []


async def test_orphan_restart_that_succeeds_changes_nothing_else() -> None:
    """RUN-8: Temporal absorbs the repeat under the same workflow id."""
    k = kit(TENANT)
    repo, item = Repo(k["scope"]), stuck()
    repo.pending[TENANT] = [item]
    starter = Starter()
    sweep, events, chain = orphan(starter, repo, k)
    out = await sweep.run()
    assert starter.started == [item.workflow_id] and out.affected == 0 and out.ok
    assert repo.failed == [] and events.events == [] and chain.entries == []


async def test_orphan_second_failure_fails_the_artefact_publishes_and_audits() -> None:
    k = kit(TENANT)
    repo, item = Repo(k["scope"]), stuck()
    repo.pending[TENANT] = [item]
    sweep, events, chain = orphan(Starter(fail=True), repo, k)
    out = await sweep.run()
    assert out.affected == 1 and repo.failed == [item.artefact_id]
    [event] = events.events
    assert event.type == "GenerationFailed" and event.tenant_id == TENANT
    assert event.payload["reason"] == "internal" and event.payload["sections_durable"] is True
    assert event.subject == f"tenders/{TENDER}"
    validate_published(event.type, event.payload)
    [entry] = chain.entries
    assert (entry.draft.entry_type, entry.draft.actor_type) == (
        audit.GENERATION_FAILED[0],
        AuditActorType.SERVICE_ACCOUNT,
    )
    assert entry.draft.actor_id == SERVICE_ACCOUNT
    assert k["directory"].asked[0][1] == NOW - timedelta(seconds=300)


# --- stalled generations ------------------------------------------------------------------------
async def test_stalled_generations_fail_internal_with_event_and_chain_entry() -> None:
    k = kit(TENANT, OTHER)
    repo, events, chain = Repo(k["scope"]), ListEvents(), FakeAuditRepo()
    repo.stalled[TENANT], repo.stalled[OTHER] = [stuck(), stuck()], [stuck(None)]
    sweep = s.StalledGeneratingArtefacts(
        FakeTx(),
        repo,
        events,
        AppendAuditEntry(chain),
        SERVICE_ACCOUNT,
        stalled_seconds=900,
        **common(k),
    )
    out = await sweep.run()
    assert out.tenants == 2 and out.affected == 3 and len(events.events) == 3
    assert {e.tenant_id for e in events.events} == {TENANT, OTHER}
    assert k["directory"].asked[0][1] == NOW - timedelta(seconds=900)
    # each tenant's artefact was processed with that tenant bound
    assert k["scope"].bound == [TENANT, OTHER]


# --- the small ones -----------------------------------------------------------------------------
async def test_idempotency_keys_are_cleared_past_the_ttl_per_tenant() -> None:
    k = kit(TENANT, OTHER)
    repo = Repo(k["scope"])
    out = await s.ClearIdempotencyKeys(FakeTx(), repo, ttl_seconds=3600, **common(k)).run()
    assert out.affected == 6 and [t for t, _ in repo.cleared] == [TENANT, OTHER]
    assert {b for _, b in repo.cleared} == {NOW - timedelta(hours=1)}


async def test_orphan_retrieval_sets_are_reclaimed_in_batches_until_a_short_one() -> None:
    k = kit(TENANT)
    repo = Repo(k["scope"])
    repo.set_batches = [500, 500, 3]
    out = await s.ReclaimOrphanRetrievalSets(FakeTx(), repo, min_age_days=30, **common(k)).run()
    assert out.affected == 1003 and repo.set_batches == []
    assert k["directory"].asked[0][1] == NOW - timedelta(days=30)


async def test_checkpoints_use_their_own_age_floor() -> None:
    k = kit(TENANT)
    repo = Repo(k["scope"])
    out = await s.SweepGraphCheckpoints(FakeTx(), repo, max_age_hours=24, **common(k)).run()
    assert out.affected == 1 and repo.checkpoints == [NOW - timedelta(hours=24)]


async def test_one_failing_tenant_is_counted_and_never_stops_the_others(
    caplog: pytest.LogCaptureFixture,
) -> None:
    k = kit(TENANT, OTHER)
    repo = Repo(k["scope"])
    repo.boom = {TENANT}
    out = await s.ClearIdempotencyKeys(FakeTx(), repo, ttl_seconds=60, **common(k)).run()
    assert out.failed_tenants == 1 and out.affected == 3 and not out.ok
    assert "failed for tenant" in caplog.text


# --- audit chain verifier -----------------------------------------------------------------------
async def chain_of(tenant: UUID, n: int) -> list[AuditEntry]:
    repo, out = FakeAuditRepo(), []
    for i in range(n):
        draft = AuditEntryDraft(
            tenant,
            NOW,
            "llm.generation.completed",
            "create",
            AuditActorType.SERVICE_ACCOUNT,
            SERVICE_ACCOUNT,
            uuid4(),
            "x",
            f"evt-{i}",
            metadata={"i": i},
        )
        e = await repo.append(None, draft)
        assert e is not None
        out.append(e)
    return out


class Chains:
    def __init__(self, scope: Scope, chains: dict[UUID, list[AuditEntry]]) -> None:
        self.scope, self.chains = scope, chains

    async def append(self, tx: Any, draft: Any) -> Any:
        raise AssertionError("the verifier never writes")

    async def list_chain(self, tx: Any, tenant_id: UUID, *, after_seq: int = 0) -> list[AuditEntry]:
        assert self.scope.current == tenant_id
        return [e for e in self.chains[tenant_id] if e.seq > after_seq]


async def test_the_verifier_is_quiet_on_an_intact_chain_and_alerts_on_the_first_break(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """AC-2 / AC-3: a flipped hash and a deleted entry are both breaks; it pages, never repairs."""
    good, bad_hash, gap = (
        await chain_of(TENANT, 4),
        await chain_of(OTHER, 4),
        await chain_of(uuid4(), 4),
    )
    bad_hash[2] = replace(bad_hash[2], payload_hash=b"\x00" * 32)
    del gap[1]
    k = kit(TENANT, OTHER, gap[0].draft.tenant_id)
    chains = Chains(k["scope"], {TENANT: good, OTHER: bad_hash, gap[0].draft.tenant_id: gap})
    out = await s.VerifyAuditChains(FakeTx(), chains, **common(k)).run()  # type: ignore[arg-type]
    assert len(out.alerts) == 2 and not out.ok and "seq 3" in out.alerts[0]
    assert "seq 3" in out.alerts[1]  # the gap shows at the first entry after the deleted one
    assert "ALERT" in caplog.text
    # a second run does not accumulate the first run's alerts
    k2 = kit(TENANT)
    sweep = s.VerifyAuditChains(FakeTx(), Chains(k2["scope"], {TENANT: good}), **common(k2))  # type: ignore[arg-type]
    clean = await sweep.run()
    assert clean.ok and clean.alerts == ()


# --- outbox and ledger --------------------------------------------------------------------------
class Pruner:
    def __init__(self, *batches: int) -> None:
        self.batches, self.calls = list(batches), []

    async def prune_published(self, older_than: timedelta, limit: int) -> int:
        self.calls.append((older_than, limit))
        return self.batches.pop(0) if self.batches else 0


async def test_published_outbox_rows_are_pruned_in_both_databases_and_dead_letters_are_not() -> (
    None
):
    corpus, generation = Pruner(1000, 4), Pruner(0)
    out = await s.PrunePublishedOutbox(
        {"corpus": corpus, "generation": generation}, older_than_days=7
    ).run()
    assert out.affected == 1004 and corpus.calls[0] == (timedelta(days=7), 1000)
    assert len(corpus.calls) == 2 and len(generation.calls) == 1


class Ledger:
    def __init__(self) -> None:
        self.retention: timedelta | None = None

    async def prune(self, retention: timedelta, batch: int = 0) -> int:
        self.retention = retention
        return 2


async def test_destructive_consumers_keep_their_ledger_rows_longer_evt_16() -> None:
    from rag.application.events.contracts import DESTRUCTIVE_CONSUMERS

    ledgers = {
        n: Ledger()
        for n in ("purge_projection", "pack_invalidation", "tenant_state", "quota_resume")
    }
    out = await s.PruneProcessedEvents(
        ledgers, DESTRUCTIVE_CONSUMERS, ordinary_days=8, destructive_days=90
    ).run()
    assert out.affected == 8
    assert ledgers["pack_invalidation"].retention == timedelta(days=8)
    assert ledgers["quota_resume"].retention == timedelta(days=8)
    assert ledgers["purge_projection"].retention == timedelta(days=90)
    assert ledgers["tenant_state"].retention == timedelta(days=90)


# --- tenant purge, phase two --------------------------------------------------------------------
def purge_world(
    changed_at: datetime,
) -> tuple[s.PurgeDueTenants, World, FakePurgeRepo, FakeStore, dict[str, Any]]:
    k = kit(TENANT)
    k["clock"] = lambda: LATER
    w = World()
    col = w.add_collection(status=CollectionStatus.PURGING)
    reference = w.add_collection(status=CollectionStatus.ACTIVE, tenant_id=OTHER, tender_id=uuid4())
    assert reference.id in w.state.collections
    w.state.lifecycle[TENANT] = TenantLifecycle(TENANT, TenantState.PURGED, changed_at, "x")
    repo = FakePurgeRepo(
        [
            make_artefact(),
            make_artefact(
                tender_id=None,
                kind=ArtefactKind.ANSWER,
            ),
        ]
    )
    store, chain = FakeStore(), FakeAuditRepo()
    purge = PurgeArtefacts(FakeTx(), repo, AppendAuditEntry(chain), store, FixedClock())
    sweep = s.PurgeDueTenants(
        w.repos,
        w.tx,
        purge,
        PurgeTenantCorpus(w.tx, w.repos, soak_days=7, clock=lambda: LATER),
        SERVICE_ACCOUNT,
        soak_days=7,
        **common(k),
    )
    assert col.id in w.state.collections
    return sweep, w, repo, store, k


async def test_tenant_purge_phase_two_deletes_after_the_soak_and_keeps_the_lifecycle_row() -> None:
    sweep, w, repo, store, k = purge_world(NOW)  # 30 days ago > 7
    out = await sweep.run()
    assert out.affected == 1 and repo.artefacts == [] and store.deleted == [(TENANT, None)]
    assert [c.tenant_id for c in w.state.collections.values()] == [OTHER]  # the tenant's rows only
    assert w.state.lifecycle[TENANT].purged  # permanent (TL-1)
    assert k["directory"].asked[0][1] == LATER - timedelta(days=7)


async def test_tenant_purge_waits_out_the_soak() -> None:
    sweep, w, repo, store, _ = purge_world(LATER - timedelta(days=2))
    out = await sweep.run()
    assert out.affected == 0 and len(repo.artefacts) == 2 and len(w.state.collections) == 2


async def test_a_failed_artefact_purge_leaves_the_corpus_rows_for_the_next_run() -> None:
    """Generation first; the lifecycle row stays due, so both halves retry together."""
    sweep, w, repo, store, _ = purge_world(NOW)

    async def boom(*a: Any, **kw: Any) -> None:
        raise RuntimeError("llm_generation down")

    repo.delete_artefact_trees = boom  # type: ignore[method-assign]
    out = await sweep.run()
    assert out.failed_tenants == 1 and len(w.state.collections) == 2


# --- stalled promotions -------------------------------------------------------------------------
async def test_a_promotion_that_never_heard_from_ingestion_moves_to_migration_failed() -> None:
    k = kit(TENANT)
    k["clock"] = lambda: LATER
    w = World()
    source = w.add_collection(status=CollectionStatus.ACTIVE)
    target = w.add_collection(
        restricted=True,
        promoted_from=source.id,
        status=CollectionStatus.PROVISIONING,
        tender_id=uuid4(),
    )
    fresh = w.add_collection(restricted=True, promoted_from=source.id, tender_id=uuid4())
    fresh = replace(fresh, created_at=LATER - timedelta(hours=1))
    w.state.collections[fresh.id] = fresh
    sweep = s.StalledPromotions(w.tx, w.repos, w.events, timeout_seconds=86400, **common(k))
    out = await sweep.run()
    assert out.affected == 1
    assert w.state.collections[target.id].status is CollectionStatus.MIGRATION_FAILED
    assert w.state.collections[fresh.id].status is CollectionStatus.PROVISIONING
    assert w.state.collections[source.id].status is CollectionStatus.ACTIVE  # keeps serving
    [event] = w.state.events("CollectionStatusChanged")
    assert event.payload["reason"] == "migration_failed"


# --- the runner's plumbing ----------------------------------------------------------------------
def test_directory_has_a_query_for_every_per_tenant_sweep() -> None:
    from rag.infrastructure.sweeps.directory import _CORPUS, _GENERATION
    from rag.infrastructure.sweeps.runner import CORPUS_SWEEPS, NAMES

    per_tenant = set(NAMES) - {s.OUTBOX, s.PROCESSED_EVENTS}
    assert per_tenant == set(_GENERATION) | set(_CORPUS) and set(_CORPUS) == CORPUS_SWEEPS


def test_lock_key_is_stable_distinct_and_a_signed_bigint() -> None:
    from rag.infrastructure.sweeps.runner import NAMES, lock_key

    keys = {n: lock_key(n) for n in NAMES}
    assert len(set(keys.values())) == len(NAMES) and keys == {n: lock_key(n) for n in NAMES}
    assert all(-(2**63) <= k < 2**63 for k in keys.values())


async def test_a_run_that_cannot_take_the_lock_skips_and_the_holder_runs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from rag.infrastructure.sweeps import runner

    class Conn:
        def __init__(self, free: bool) -> None:
            self.free, self.sql = free, []

        async def fetchval(self, sql: str, key: int) -> bool:
            self.sql.append(sql)
            return self.free

    async def fake_run_in_tx(pool: Any, fn: Any) -> Any:
        return await fn(pool)

    monkeypatch.setattr(runner, "run_in_tx", fake_run_in_tx)

    class Swept:
        ran = False

        async def run(self) -> s.SweepOutcome:
            Swept.ran = True
            return s.SweepOutcome("x", affected=1)

    held = await runner.run_exclusive(Conn(False), "x", Swept())  # type: ignore[arg-type]
    assert held.skipped and not Swept.ran
    free_conn = Conn(True)
    assert (await runner.run_exclusive(free_conn, "x", Swept())).affected == 1  # type: ignore[arg-type]
    assert "pg_try_advisory_xact_lock" in free_conn.sql[0]  # transaction-scoped: PgBouncer-safe


async def test_the_cli_rejects_an_unknown_sweep_and_the_runner_requires_enumeration_dsns() -> None:
    from rag.infrastructure.config import Settings
    from rag.infrastructure.sweeps.__main__ import main
    from rag.infrastructure.sweeps.runner import open_pools

    assert await main(["nope"]) == 64
    with pytest.raises(RuntimeError, match="rag_sweeper"):
        await open_pools(Settings())  # without them a tenant list silently comes back empty
    with pytest.raises(RuntimeError, match="at least 2"):
        await open_pools(
            Settings(
                corpus_sweep_database_url="x",
                generation_sweep_database_url="y",
                sweep_pool_max_conns=1,
            )
        )
