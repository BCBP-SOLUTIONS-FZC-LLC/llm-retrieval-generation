"""An in-memory world for the workflows and regeneration use-case tests.

One ``World`` holds the rows; the repositories are views over it. ``WorldTx`` snapshots the world
and restores it when the unit of work raises, so "one transaction" is observable: a failure in the
last statement leaves nothing behind (rows, audit entries and outbox events alike).
"""

from __future__ import annotations

import copy
import json
from collections.abc import Awaitable, Callable
from dataclasses import replace
from typing import Any
from uuid import UUID, uuid4

from rag.application.events.contracts import PUBLISHED
from rag.application.generation.commands import (
    AppendAuditEntry,
    MarkVersionsSuperseded,
)
from rag.application.generation.flow import (
    AdmitGeneration,
    CompleteGeneration,
    GenerationSettings,
    RunLifecycle,
)
from rag.application.generation.generate import ShortFormEngine
from rag.application.generation.refiner import InstructionRefiner
from rag.application.generation.regenerate import (
    RegenerateDocument,
    RegenerateSection,
    RegenerationDeps,
    ResolveFactConflict,
)
from rag.application.shared.events import DomainEvent
from rag.application.shared.gateway import Completion, GatewayContext
from rag.application.verification.verify import VerificationConfig, VerifySection
from rag.application.workflows.bridges import QuotaRunResumer
from rag.application.workflows.common import RunActions
from rag.application.workflows.operator import RematerialiseRun
from rag.application.workflows.ports import StartOutcome, StartRequest
from rag.application.workflows.signals import (
    ApproveOutline,
    CancelGeneration,
    ResumeGeneration,
    ReviseOutline,
)
from rag.application.workflows.start import (
    QueueNames,
    RunStarter,
    StartAsyncExtraction,
    StartGeneration,
)
from rag.application.workflows.state import GetGenerationState
from rag.domain.generation.artefact import Artefact, ArtefactVersion
from rag.domain.generation.facts import DocumentFact, FactConflict
from rag.domain.generation.instruction import InstructionRefinement
from rag.domain.generation.repositories import CompletionRows, IdempotencyRace
from rag.domain.generation.run import GenerationRun
from rag.domain.generation.section import ArtefactSection, SectionVersion
from rag.domain.shared.enums import (
    ArtefactKind,
    ArtefactStatus,
    EligibilityState,
    GenerationMode,
    OutlineState,
    RunFailureReason,
    RunStage,
    RunStatus,
    SectionState,
)
from rag.domain.shared.errors import DomainError, ErrorCode
from rag.domain.shared.identity import RequestIdentity
from rag.domain.workflows.views import RunStateView, SectionProgress
from rag.infrastructure.events.schemas import validate_published
from tests.support_generate import SCHEMA, Binder, Gate, Probe, Store, StreamGateway, UserCtx
from tests.support_generation import (
    NOW,
    TENANT,
    TENDER,
    USER,
    FakeAuditRepo,
    FakeScope,
    FixedClock,
    make_artefact,
    make_section,
    make_section_version,
    make_version,
)
from tests.support_retrieval import Rig, bundle, section_profile

__all__ = [
    "NOW",
    "TENANT",
    "TENDER",
    "USER",
    "FakeWorkflows",
    "World",
    "make_run",
    "seed_document",
]


class _Audit(FakeAuditRepo):
    """The chain, with an outage a test can switch on to fail the last statement of a unit."""

    fail = False

    async def append(self, tx: Any, draft: Any) -> Any:
        if self.fail:
            raise RuntimeError("chain unavailable")
        return await super().append(tx, draft)


class World:
    """Every row the slice touches, plus the outbox and the audit chain."""

    def __init__(self) -> None:
        self.artefacts: dict[UUID, Artefact] = {}
        self.runs: dict[UUID, GenerationRun] = {}
        self.sections: dict[UUID, ArtefactSection] = {}
        self.section_versions: dict[UUID, SectionVersion] = {}
        self.versions: dict[UUID, ArtefactVersion] = {}
        self.holds: set[tuple[UUID, UUID]] = set()
        self.facts: list[DocumentFact] = []
        self.conflicts: dict[UUID, FactConflict] = {}
        self.refinements: list[InstructionRefinement] = []
        self.completions: list[CompletionRows] = []
        self.superseded: list[UUID] = []
        self.events: list[DomainEvent] = []
        self.audit = _Audit()
        self.scope_narrowed = False

    _STATE = (
        "artefacts",
        "runs",
        "sections",
        "section_versions",
        "versions",
        "holds",
        "facts",
        "conflicts",
        "refinements",
        "completions",
        "superseded",
        "events",
    )

    def snapshot(self) -> dict[str, Any]:
        """A deep copy of every mutable member."""
        snap = {n: copy.deepcopy(getattr(self, n)) for n in self._STATE}
        snap["audit"] = list(self.audit.entries)
        return snap

    def restore(self, snap: dict[str, Any]) -> None:
        """Put a snapshot back (a rolled-back transaction)."""
        for n in self._STATE:
            setattr(self, n, snap[n])
        self.audit.entries = snap["audit"]

    def types(self) -> list[str]:
        """Published event types, in order."""
        return [e.type for e in self.events]


class WorldTx:
    """``TransactionRunner`` with rollback."""

    def __init__(self, world: World) -> None:
        self.world = world
        self.runs = 0
        self.fail_next_write: Exception | None = None

    async def run_in_tx[T](
        self, fn: Callable[[Any], Awaitable[T]], *, read_only: bool = False
    ) -> T:
        self.runs += 1
        snap = self.world.snapshot()
        try:
            return await fn(None)
        except BaseException:
            self.world.restore(snap)
            raise


class WorldEvents:
    """``EventPublisher`` validating against the published schemas, like the outbox (EVT-9)."""

    def __init__(self, world: World) -> None:
        self.world = world
        self.fail_on: str | None = None

    async def publish(self, tx: Any, event: DomainEvent) -> None:
        if event.type == self.fail_on:
            raise RuntimeError("outbox unavailable")
        assert event.type in PUBLISHED
        validate_published(event.type, event.payload)
        self.world.events.append(event)


class WorldRuns:
    """``RunRepository`` over the world."""

    def __init__(self, world: World) -> None:
        self.w = world

    async def get_run(self, tx: Any, run_id: UUID, *, lock: bool = False) -> GenerationRun | None:
        run = self.w.runs.get(run_id)
        return None if run is None else replace(run)

    async def find_run_by_workflow_id(self, tx: Any, workflow_id: str) -> GenerationRun | None:
        found = next((r for r in self.w.runs.values() if r.workflow_id == workflow_id), None)
        return None if found is None else replace(found)

    async def lock_workflow_id(self, tx: Any, workflow_id: str) -> None:
        return None

    async def save_gate(
        self,
        tx: Any,
        run: GenerationRun,
        *,
        expected_status: RunStatus,
        expected_record_version: int,
    ) -> int | None:
        cur = self.w.runs.get(run.id)
        if cur is None or cur.status is not expected_status:
            return None
        if cur.record_version != expected_record_version:
            return None
        self.w.runs[run.id] = replace(run, record_version=cur.record_version + 1)
        return cur.record_version + 1

    async def get_artefact(
        self, tx: Any, artefact_id: UUID, *, lock: bool = False
    ) -> Artefact | None:
        a = self.w.artefacts.get(artefact_id)
        return None if a is None else replace(a)

    async def save_artefact_status(self, tx: Any, artefact: Artefact) -> None:
        self.w.artefacts[artefact.id] = replace(artefact)

    async def close_in_flight_sections(
        self, tx: Any, artefact_id: UUID, failure: RunFailureReason
    ) -> int:
        n = 0
        for sid, s in list(self.w.sections.items()):
            if s.artefact_id == artefact_id and s.state is SectionState.GENERATING:
                has = s.current_version_id is not None
                self.w.sections[sid] = replace(
                    s,
                    state=SectionState.COMPLETE if has else SectionState.FAILED,
                    failure_reason=None if has else failure,
                )
                n += 1
        return n

    async def count_sections(self, tx: Any, artefact_id: UUID) -> tuple[int, int]:
        mine = [s for s in self.w.sections.values() if s.artefact_id == artefact_id]
        return sum(1 for s in mine if s.state is SectionState.COMPLETE), len(mine)

    async def read_state(self, tx: Any, run_id: UUID) -> RunStateView | None:
        run = self.w.runs.get(run_id)
        if run is None:
            return None
        art = self.w.artefacts[run.artefact_id]
        mine = sorted(
            (s for s in self.w.sections.values() if s.artefact_id == art.id),
            key=lambda s: s.ordinal,
        )
        progress = []
        for s in mine:
            sv = self.w.section_versions.get(s.current_version_id) if s.current_version_id else None
            progress.append(
                SectionProgress(s, None if sv is None else sv.evidence_tier,
                                None if sv is None else sv.retrieval_set_id)
            )  # fmt: skip
        facts = tuple(f for f in self.w.facts if f.artefact_id == art.id and not f.superseded_at)
        return RunStateView(run, art, tuple(progress), facts, self.w.scope_narrowed)


class WorldWrites:
    """``GenerationWriteRepository`` over the world (admission, completion)."""

    def __init__(self, world: World) -> None:
        self.w = world

    async def find_run_by_key(
        self, tx: Any, tenant_id: UUID, principal_id: UUID, key: str
    ) -> GenerationRun | None:
        found = next(
            (
                r
                for r in self.w.runs.values()
                if (r.tenant_id, r.principal_id, r.idempotency_key)
                == (tenant_id, principal_id, key)
            ),
            None,
        )
        return None if found is None else replace(found)

    async def release_key(self, tx: Any, run_id: UUID) -> None:
        self.w.runs[run_id] = replace(self.w.runs[run_id], idempotency_key=None, request_hash=None)

    async def insert_shell(
        self, tx: Any, artefact: Artefact, run: GenerationRun, section: ArtefactSection | None
    ) -> None:
        await self.insert_run(tx, run)
        self.w.artefacts[artefact.id] = replace(artefact)
        if section is not None:
            self.w.sections[section.id] = replace(section)

    async def insert_run(self, tx: Any, run: GenerationRun) -> None:
        if run.idempotency_key and await self.find_run_by_key(
            tx, run.tenant_id, run.principal_id, run.idempotency_key
        ):
            raise IdempotencyRace
        if run.workflow_id and any(r.workflow_id == run.workflow_id for r in self.w.runs.values()):
            raise IdempotencyRace
        self.w.runs[run.id] = replace(run)

    async def save_state(
        self, tx: Any, artefact: Artefact, run: GenerationRun, section: ArtefactSection
    ) -> None:
        self.w.artefacts[artefact.id] = replace(artefact)
        self.w.runs[run.id] = replace(run)
        self.w.sections[section.id] = replace(section)

    async def save_run_and_section(
        self, tx: Any, run: GenerationRun, section: ArtefactSection
    ) -> None:
        self.w.runs[run.id] = replace(run)
        self.w.sections[section.id] = replace(section)

    async def next_version_numbers(
        self, tx: Any, artefact_id: UUID, section_id: UUID
    ) -> tuple[int, int]:
        docs = [v for v in self.w.versions.values() if v.artefact_id == artefact_id]
        sv = [v for v in self.w.section_versions.values() if v.section_id == section_id]
        return len(docs) + 1, len(sv) + 1

    async def insert_completion(self, tx: Any, rows: CompletionRows) -> None:
        self.w.completions.append(rows)
        self.w.section_versions[rows.section_version.id] = rows.section_version
        if rows.artefact_version is not None:
            self.w.versions[rows.artefact_version.id] = rows.artefact_version

    async def count_sections(self, tx: Any, artefact_id: UUID) -> tuple[int, int]:
        return await WorldRuns(self.w).count_sections(tx, artefact_id)


class WorldReads:
    """The ``get_artefact`` the regeneration routes need."""

    def __init__(self, world: World) -> None:
        self.w = world

    async def get_artefact(self, tx: Any, artefact_id: UUID) -> Artefact | None:
        a = self.w.artefacts.get(artefact_id)
        return None if a is None else replace(a)


class WorldRegen:
    """``RegenerationRepository`` over the world."""

    def __init__(self, world: World) -> None:
        self.w = world

    async def get_section(
        self, tx: Any, artefact_id: UUID, ref: str
    ) -> tuple[ArtefactSection, SectionVersion | None] | None:
        s = next(
            (
                s
                for s in self.w.sections.values()
                if s.artefact_id == artefact_id and ref in (s.tender_section_ref, str(s.id))
            ),
            None,
        )
        if s is None:
            return None
        return replace(s), self.w.section_versions.get(s.current_version_id or uuid4())

    async def lock_section(self, tx: Any, section_id: UUID) -> ArtefactSection | None:
        s = self.w.sections.get(section_id)
        return None if s is None else replace(s)

    async def document_holds_section_version(
        self, tx: Any, artefact_version_id: UUID, section_version_id: UUID
    ) -> bool:
        return (artefact_version_id, section_version_id) in self.w.holds

    async def get_version(self, tx: Any, artefact_version_id: UUID) -> ArtefactVersion | None:
        return self.w.versions.get(artefact_version_id)

    async def mark_superseded_by_section_edit(
        self, tx: Any, artefact_id: UUID, section_id: UUID
    ) -> list[UUID]:
        mine = {sv.id for sv in self.w.section_versions.values() if sv.section_id == section_id}
        hit = [
            v
            for (v, sv) in sorted(self.w.holds, key=str)
            if sv in mine and v not in self.w.superseded
        ]
        self.w.superseded.extend(hit)
        for v in hit:
            self.w.versions[v] = replace(self.w.versions[v], superseded_by_section_edit=True)
        return hit


class WorldFacts:
    """``FactRepository`` over the world."""

    def __init__(self, world: World) -> None:
        self.w = world

    async def current_facts(self, tx: Any, artefact_id: UUID) -> list[DocumentFact]:
        return [f for f in self.w.facts if f.artefact_id == artefact_id and not f.superseded_at]

    async def insert_conflict(self, tx: Any, conflict: FactConflict) -> None:
        self.w.conflicts[conflict.id] = replace(conflict)

    async def get_conflict(
        self, tx: Any, conflict_id: UUID, *, lock: bool = False
    ) -> FactConflict | None:
        c = self.w.conflicts.get(conflict_id)
        return None if c is None else replace(c)

    async def find_resolved(
        self, tx: Any, section_id: UUID, fact_key: str, instruction_value: str
    ) -> FactConflict | None:
        return next(
            (
                replace(c)
                for c in self.w.conflicts.values()
                if (c.section_id, c.fact_key, c.instruction_value)
                == (section_id, fact_key, instruction_value)
                and c.resolution is not None
            ),
            None,
        )

    async def save_resolution(self, tx: Any, conflict: FactConflict) -> bool:
        cur = self.w.conflicts[conflict.id]
        if cur.resolution is not None:
            return False
        self.w.conflicts[conflict.id] = replace(conflict)
        return True

    async def supersede_fact(self, tx: Any, artefact_id: UUID, fact_key: str, at: Any) -> bool:
        for i, f in enumerate(self.w.facts):
            if f.artefact_id == artefact_id and f.fact_key == fact_key and not f.superseded_at:
                self.w.facts[i] = replace(f, superseded_at=at)
                return True
        return False

    async def insert_fact(self, tx: Any, fact: DocumentFact) -> None:
        if any(
            f.artefact_id == fact.artefact_id
            and f.fact_key == fact.fact_key
            and not f.superseded_at
            for f in self.w.facts
        ):
            raise AssertionError("uq_fact_current: supersede the old fact first")
        self.w.facts.append(fact)

    async def flag_sections(
        self, tx: Any, artefact_id: UUID, *, except_section_id: UUID, reason: str
    ) -> list[UUID]:
        out: list[UUID] = []
        for sid, s in list(self.w.sections.items()):
            if (
                s.artefact_id == artefact_id
                and sid != except_section_id
                and s.current_version_id is not None
                and not s.flagged_for_review
            ):
                self.w.sections[sid] = replace(s, flagged_for_review=True, flagged_reason=reason)
                out.append(sid)
        return out


class WorldRefinements:
    """``InstructionRefinementRepository`` over the world."""

    def __init__(self, world: World) -> None:
        self.w = world

    async def insert(self, tx: Any, row: InstructionRefinement) -> None:
        self.w.refinements.append(row)

    async def record_resolution(self, tx: Any, fact_conflict_id: UUID, resolution: str) -> None:
        from rag.domain.shared.enums import ConflictResolution

        for i, r in enumerate(self.w.refinements):
            if r.fact_conflict_id == fact_conflict_id:
                self.w.refinements[i] = replace(
                    r, conflict_resolution=ConflictResolution(resolution)
                )


class FakeWorkflows:
    """``WorkflowClient`` recording starts and signals, with every failure scriptable."""

    def __init__(self, *, available: bool = True) -> None:
        self.available = available
        self.starts: list[StartRequest] = []
        self.signals: list[tuple[str, str, dict[str, Any] | None]] = []
        self.fail_start: DomainError | None = None
        self.fail_signal: DomainError | None = None
        self.live: set[str] = set()
        """Workflow ids the engine already holds: a start under one is ``already_started``."""

    async def start(self, request: StartRequest) -> StartOutcome:
        if self.fail_start is not None:
            raise self.fail_start
        self.starts.append(request)
        if request.workflow_id in self.live:
            return StartOutcome.ALREADY_STARTED
        self.live.add(request.workflow_id)
        return StartOutcome.STARTED

    async def signal(
        self, workflow_id: str, signal: str, payload: dict[str, Any] | None = None
    ) -> None:
        if self.fail_signal is not None:
            raise self.fail_signal
        self.signals.append((workflow_id, signal, payload))


def engine_down() -> DomainError:
    """What the real client raises when Temporal is unreachable."""
    return DomainError(ErrorCode.WORKFLOW_ENGINE_UNAVAILABLE)


def make_run(artefact: Artefact, **kw: Any) -> GenerationRun:
    """A long-form run of ``artefact`` (``pending`` unless told otherwise)."""
    base: dict[str, Any] = dict(
        id=uuid4(),
        artefact_id=artefact.id,
        tenant_id=artefact.tenant_id,
        principal_id=USER,
        tender_id=artefact.tender_id,
        requested_mode=GenerationMode.ASSISTANCE,
        effective_mode=GenerationMode.ASSISTANCE,
        eligibility_state=EligibilityState.GENERATABLE,
        domain_profile_version="dp-1",
        language_pack_version="lp-1",
        workflow_id=str(artefact.id),
        run_input={"plan": "standard", "residency_mode": "standard", "kind": "document"},
        created_at=NOW,
    )
    return GenerationRun(**{**base, **kw})


def seed_document(
    world: World,
    *,
    status: RunStatus = RunStatus.AWAITING_OUTLINE_APPROVAL,
    artefact_status: ArtefactStatus = ArtefactStatus.GENERATING,
    sections: int = 3,
    complete: int = 0,
    generating: int = 0,
    outline_version: int | None = 1,
    outline_state: OutlineState | None = None,
    failure_reason: RunFailureReason | None = None,
    **run_kw: Any,
) -> tuple[Artefact, GenerationRun, list[ArtefactSection]]:
    """A long-form artefact, its run and ``sections`` rows (some complete, some generating)."""
    artefact = make_artefact(status=artefact_status)
    if outline_state is None:
        outline_state = (
            OutlineState.READY
            if status is RunStatus.AWAITING_OUTLINE_APPROVAL
            else OutlineState.NONE
        )
    run = make_run(
        artefact,
        status=status,
        outline_state=outline_state,
        outline_version=outline_version if outline_state is not OutlineState.NONE else None,
        failure_reason=failure_reason,
        current_stage=RunStage.OUTLINE,
        **run_kw,
    )
    world.artefacts[artefact.id] = artefact
    world.runs[run.id] = run
    rows: list[ArtefactSection] = []
    for i in range(1, sections + 1):
        s = make_section(artefact.id, ordinal=i)
        if i <= complete:
            sv = make_section_version(s.id, uuid4(), content=f"s{i}".encode(), version_no=1)
            world.section_versions[sv.id] = sv
            s = replace(s, state=SectionState.COMPLETE, current_version_id=sv.id)
        elif i <= complete + generating:
            s = replace(s, state=SectionState.GENERATING)
        world.sections[s.id] = s
        rows.append(s)
    return artefact, run, rows


# --- the wired rig ------------------------------------------------------------


class WfGateway(StreamGateway):
    """``StreamGateway`` that also answers the ``refine-instruction`` task class."""

    def __init__(self) -> None:
        super().__init__()
        self.instruction_text = json.dumps(
            {
                "instruction_type": "add",
                "refined_instruction": "Cite the night-work restriction.",
                "needs_retrieval": True,
                "fact_conflict": None,
            }
        )
        self.instruction_error: Exception | None = None
        self.instruction_calls: list[str] = []

    async def complete(  # type: ignore[override]
        self,
        task_class: str,
        system: str,
        user: str,
        *,
        ctx: GatewayContext,
        deadline_seconds: float,
    ) -> Completion:
        if task_class == "refine-instruction":
            self.instruction_calls.append(user)
            if self.instruction_error is not None:
                raise self.instruction_error
            return Completion(self.instruction_text, "refiner-model")
        return await super().complete(
            task_class, system, user, ctx=ctx, deadline_seconds=deadline_seconds
        )


class Directory:
    """``PrincipalDirectory``: the identity IAM would return, or an outage."""

    def __init__(self) -> None:
        self.error: DomainError | None = None
        self.calls: list[tuple[UUID, UUID, str | None, str]] = []
        self.roles: frozenset[str] = frozenset()

    async def identity_for(
        self, tenant_id: UUID, principal_id: UUID, *, plan: str | None, residency_mode: str
    ) -> RequestIdentity:
        self.calls.append((tenant_id, principal_id, plan, residency_mode))
        if self.error is not None:
            raise self.error
        return RequestIdentity(tenant_id=tenant_id, principal_id=principal_id, roles=self.roles)


class WfRig:
    """Every workflows and regeneration use case over one ``World``."""

    def __init__(self, *, profile: Any = None, packs_kw: dict[str, Any] | None = None) -> None:
        self.world = World()
        self.tx, self.events = WorldTx(self.world), WorldEvents(self.world)
        self.gw = WfGateway()
        self.base = Rig(gateway=self.gw)
        self.packs = self.base.packs
        self.packs.bundle = bundle(
            profile or section_profile(),
            **{"extraction_schemas": {SCHEMA.ref: SCHEMA}, **(packs_kw or {})},
        )
        self.runs, self.writes = WorldRuns(self.world), WorldWrites(self.world)
        self.regen, self.facts = WorldRegen(self.world), WorldFacts(self.world)
        self.refinements, self.reads = WorldRefinements(self.world), WorldReads(self.world)
        self.probe, self.gate, self.store = Probe(), Gate(), Store()
        self.scope, self.directory = FakeScope(), Directory()
        self.workflows = FakeWorkflows()
        self.binder = Binder()
        clock = FixedClock()
        self.cfg = GenerationSettings(heartbeat_seconds=15.0, idempotency_ttl_seconds=3600)
        self.admit = AdmitGeneration(
            self.tx, self.writes, self.base.provider, self.packs, self.gw, self.probe, self.gate,
            self.events, clock, self.cfg,
        )  # fmt: skip
        self.audit = AppendAuditEntry(self.world.audit)
        self.lifecycle = RunLifecycle(self.tx, self.writes, self.audit, clock)
        self.complete = CompleteGeneration(
            self.tx, self.writes, self.store, self.audit, self.events, clock,
            MarkVersionsSuperseded(self.regen),  # type: ignore[arg-type]
        )  # fmt: skip
        self.verify = VerifySection(VerificationConfig(tier0_sample_rate=0.0), rand=lambda: 1.0)
        self.engine = ShortFormEngine(
            admit=self.admit, lifecycle=self.lifecycle, complete=self.complete,
            pipeline=self.base.pipeline, recorder=self.base.recorder, gateway=self.gw,
            verify=self.verify, reads=self.reads,  # type: ignore[arg-type]
            tx=self.tx, binder=self.binder, settings=self.cfg, user_context=UserCtx(),
            tokens=self.gw,
        )  # fmt: skip
        self.queues = QueueNames("q", "q-ent", "q-strict")
        starter = RunStarter(self.workflows, self.queues)
        self.starter = starter
        self.start = StartGeneration(self.admit, starter)
        self.start_extraction = StartAsyncExtraction(self.admit, starter)
        self.state = GetGenerationState(self.tx, self.runs, self.scope)
        self.actions = RunActions(
            tx=self.tx, runs=self.runs, scope=self.scope, packs=self.packs,
            workflows=self.workflows, events=self.events, audit_log=self.audit, clock=clock,
        )  # fmt: skip
        self.approve, self.revise = ApproveOutline(self.actions), ReviseOutline(self.actions)
        self.cancel, self.resume = CancelGeneration(self.actions), ResumeGeneration(self.actions)
        self.rematerialise = RematerialiseRun(
            self.tx, self.runs, self.workflows, self.queues, self.audit, clock
        )
        self.resumer = QuotaRunResumer(
            self.tx,
            self.runs,
            self.resume,
            self.directory,
            self.workflows,  # type: ignore[arg-type]
        )
        deps = RegenerationDeps(
            self.tx,
            self.reads,
            self.regen,
            self.scope,
            clock,  # type: ignore[arg-type]
        )
        self.refiner = InstructionRefiner(self.gw, enabled=True, deadline_seconds=3.0)
        self.regenerate_document = RegenerateDocument(
            deps, self.admit, self.writes, self.runs.find_run_by_workflow_id,
            self.runs.lock_workflow_id, self.events, starter,
        )  # fmt: skip
        self.regenerate_section = RegenerateSection(
            deps, self.admit, self.engine, self.lifecycle, self.writes, self.facts,
            self.refinements, self.refiner, self.store, self.events, self.cfg,
        )  # fmt: skip
        self.resolve_conflict = ResolveFactConflict(
            deps, self.facts, self.refinements, self.audit, self.gate
        )

    async def settle(self) -> None:
        """Wait for every detached producer to finish."""
        import asyncio

        tasks = list(self.engine._tasks)
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)


__all__ += ["WfRig", "WfGateway", "Directory", "SCHEMA"]
__all__ += ["WorldTx", "WorldEvents", "WorldRuns", "WorldWrites", "WorldReads", "WorldRegen"]
__all__ += ["WorldFacts", "WorldRefinements", "engine_down", "make_version", "ArtefactKind"]
