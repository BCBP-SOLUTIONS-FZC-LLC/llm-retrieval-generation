"""A-5, A-6 and A-7: regeneration and fact-conflict resolution (LLD §5.7.9, §5.7.10, §10, §13.9).

A-6 streams one section through the same engine as R-3 (admission, the full retrieval pipeline,
the §8.1 ladder and §8.2 checklist: REGEN-7, REGEN-9) and mints a **section** version only
(REGEN-8). A-5 is document scope: it opens a run row and starts a workflow under
``regen:{base_version_id}`` (RUN-2) and answers with a handle, like W-1. A-7 closes a surfaced
fact conflict and never regenerates (LD-26).

Deviations, all recorded in ``docs/implementation-plan.md``: the style-only reuse of the prior
retrieval set (REGEN-5) is not built because a stored chunk carries a 240-character excerpt, not
prompt text, so every regeneration takes the full pipeline (safe under REGEN-9); and A-6's
``base_version_id`` is a section version id, or a document version that still holds the
section's current version (the LLD does not say which).
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime
from uuid import UUID, uuid4

from rag.application.events.contracts import (
    GenerationStarted,
    Recipient,
    build_event,
    tender_subject,
)
from rag.application.generation.commands import AppendAuditEntry
from rag.application.generation.flow import (
    AdmissionRequest,
    AdmitGeneration,
    Admitted,
    GenerationSettings,
    RunLifecycle,
    SectionRegeneration,
)
from rag.application.generation.frames import FactConflictOut, VerdictFrame
from rag.application.generation.generate import GenerationStream, ShortFormEngine, ShortFormSpec
from rag.application.generation.ports import ArtefactContentStore, TenderGate
from rag.application.generation.refiner import InstructionRefiner, RefineRun, principal_hash
from rag.application.retrieval.scope import ScopeSpec
from rag.application.shared.events import EventPublisher
from rag.application.shared.gateway import GatewayContext
from rag.application.shared.ports import TransactionRunner, TxHandle
from rag.application.shared.scope import ScopeAuthorizer, ScopeTarget
from rag.application.workflows.observability import NullWorkflowObserver, WorkflowObserver
from rag.application.workflows.ports import WORKFLOW_GENERATION
from rag.application.workflows.start import RunHandle, RunStarter, handle_of
from rag.domain.generation import audit
from rag.domain.generation import regeneration as rules
from rag.domain.generation.artefact import Artefact
from rag.domain.generation.eligibility import EligibilityVerdict
from rag.domain.generation.facts import (
    ConflictOutcome,
    DocumentFact,
    FactConflict,
    resolve_conflict,
)
from rag.domain.generation.instruction import InstructionRefinement
from rag.domain.generation.repositories import (
    ArtefactReadRepository,
    Clock,
    FactRepository,
    GenerationWriteRepository,
    IdempotencyRace,
    InstructionRefinementRepository,
    RegenerationRepository,
)
from rag.domain.generation.run import GenerationRun
from rag.domain.generation.section import ArtefactSection, SectionVersion
from rag.domain.packs.profile import PackBundle
from rag.domain.retrieval.plan import DegradationMarker
from rag.domain.shared.enums import (
    ArtefactOrigin,
    AuditActorType,
    ConflictResolution,
    DeliveryState,
    EvidenceTier,
    GenerationMode,
    GrantLevel,
    SectionState,
)
from rag.domain.shared.errors import DomainError, ErrorCode
from rag.domain.shared.identity import RequestIdentity
from rag.domain.workflows.ids import regeneration_workflow_id
from rag.domain.workflows.input import ScopeSeed, WorkflowInput

log = logging.getLogger(__name__)
MAX_REVIEW_CONTEXT_CHARS = 8000


@dataclass(frozen=True, slots=True)
class RegenerateSectionCommand:
    """A-6 after validation."""

    artefact_id: UUID
    section_ref: str
    base_version_id: UUID
    instruction: str | None = None
    review_context: dict[str, object] | None = None
    """Prompt input for this request only: never persisted, never cached (REGEN-3)."""
    recipients: tuple[Recipient, ...] = ()
    correlation_id: str = ""


@dataclass(frozen=True, slots=True)
class RegenerateDocumentCommand:
    """A-5 after validation. No review context: A-5's run input is stored (REGEN-3)."""

    artefact_id: UUID
    base_version_id: UUID
    instruction: str | None = None
    recipients: tuple[Recipient, ...] = ()
    correlation_id: str = ""


@dataclass(frozen=True, slots=True)
class RegenerationDeps:
    """What A-5, A-6 and A-7 share."""

    tx: TransactionRunner
    reads: ArtefactReadRepository
    regen: RegenerationRepository
    scope: ScopeAuthorizer
    clock: Clock
    observer: WorkflowObserver = field(default_factory=NullWorkflowObserver)


def _not_found() -> DomainError:
    return DomainError(ErrorCode.ARTEFACT_NOT_FOUND, "The artefact was not found.")


async def authorized_artefact(
    d: RegenerationDeps, identity: RequestIdentity, artefact_id: UUID
) -> Artefact:
    """The artefact, after scope is re-resolved at ``generate`` (API-10).

    An empty scope is the uniform ``404`` because an artefact route never reveals a collection
    (LD-62).
    """
    artefact = await d.tx.run_in_tx(
        lambda tx: d.reads.get_artefact(tx, artefact_id), read_only=True
    )
    if artefact is None:
        raise _not_found()
    try:
        await d.scope.require_level(
            identity, ScopeTarget(tender_id=artefact.tender_id), GrantLevel.GENERATE
        )
    except DomainError as exc:
        if exc.code is ErrorCode.COLLECTION_NOT_FOUND:
            raise _not_found() from exc
        raise
    return artefact


def _admission_scope_error(exc: DomainError) -> DomainError:
    """``prepare`` re-resolves scope itself; an artefact route turns its empty result to 404."""
    if exc.code is ErrorCode.COLLECTION_NOT_FOUND:
        return _not_found()
    return exc


# --- A-5 -------------------------------------------------------------------------------------
class RegenerateDocument:
    """A-5 ``POST /api/v1/artefacts/{id}/regenerate``.

    A run row, then a workflow under ``regen:{base_version_id}``; a repeat from one base version is
    absorbed (RUN-2).
    """

    def __init__(
        self,
        deps: RegenerationDeps,
        admit: AdmitGeneration,
        write: GenerationWriteRepository,
        runs_by_workflow: Callable[[TxHandle, str], Awaitable[GenerationRun | None]],
        lock_workflow: Callable[[TxHandle, str], Awaitable[None]],
        events: EventPublisher,
        starter: RunStarter,
    ) -> None:
        self._d, self._admit, self._write = deps, admit, write
        self._find, self._lock = runs_by_workflow, lock_workflow
        self._events, self._starter = events, starter

    async def execute(self, identity: RequestIdentity, cmd: RegenerateDocumentCommand) -> RunHandle:
        """``202`` for a new run, the existing handle (``replayed``) for a repeat."""
        d = self._d
        artefact = await authorized_artefact(d, identity, cmd.artefact_id)
        workflow_id = regeneration_workflow_id(cmd.base_version_id)
        existing = await d.tx.run_in_tx(lambda tx: self._find(tx, workflow_id), read_only=True)
        if existing is not None and existing.artefact_id == artefact.id:
            return await self._existing(existing)
        rules.guard_document_base(artefact, cmd.base_version_id)  # REGEN-2
        rules.require_regenerable(artefact)
        self._starter.require_engine()
        instruction = (cmd.instruction or "").strip() or None
        request = AdmissionRequest(
            kind=artefact.kind,
            capability=artefact.capability,
            section_type=artefact.capability,
            user_text=instruction or "",
            scope=ScopeSpec(tender_id=artefact.tender_id),
            required_level=GrantLevel.GENERATE,
            tender_id=artefact.tender_id,
            correlation_id=cmd.correlation_id,
            target_ref=artefact.target_ref,
            recipients=cmd.recipients,
        )
        try:
            prep = await self._admit.prepare(identity, request)
        except DomainError as exc:
            raise _admission_scope_error(exc) from exc
        try:
            run = await d.tx.run_in_tx(
                lambda tx: self._commit(
                    tx,
                    identity,
                    artefact,
                    cmd,
                    instruction,
                    prep.bundle,
                    prep.eligibility,
                    request,
                    workflow_id,
                )
            )
        except IdempotencyRace:
            found = await d.tx.run_in_tx(lambda tx: self._find(tx, workflow_id), read_only=True)
            if found is None:
                raise
            return await self._existing(found)
        d.observer.regeneration("document", "instruction" if instruction else "retry")
        await self._starter.begin(run, WORKFLOW_GENERATION)
        return handle_of(run, replayed=False)

    async def _existing(self, run: GenerationRun) -> RunHandle:
        if run.status.value == "pending":  # a start that failed after the commit: try again
            await self._starter.begin(run, WORKFLOW_GENERATION)
        return handle_of(run, replayed=True)

    async def _commit(
        self,
        tx: TxHandle,
        identity: RequestIdentity,
        artefact: Artefact,
        cmd: RegenerateDocumentCommand,
        instruction: str | None,
        bundle: PackBundle,
        eligibility: EligibilityVerdict,
        request: AdmissionRequest,
        workflow_id: str,
    ) -> GenerationRun:
        assert identity.principal_id is not None
        await self._lock(tx, workflow_id)  # concurrent repeats take turns and see the first row
        again = await self._find(tx, workflow_id)
        if again is not None:
            raise IdempotencyRace
        run_id = uuid4()
        winput = WorkflowInput(
            tenant_id=identity.tenant_id,
            principal_id=identity.principal_id,
            artefact_id=artefact.id,
            generation_run_id=run_id,
            capability=artefact.capability,
            requested_mode=eligibility.requested_mode.value,
            plan=identity.plan,
            residency_mode=identity.residency_mode.value,
            domain_profile_version=bundle.domain_profile.version,
            language_pack_version=bundle.language_pack.version,
            scope_seed=ScopeSeed(artefact.tender_id),
            tender_id=artefact.tender_id,
            target_ref=artefact.target_ref,
            correlation_id=cmd.correlation_id,
            kind="document",
            outline_gate=False,
            origin=ArtefactOrigin.REGENERATION.value,
            base_version_id=cmd.base_version_id,
            instruction=instruction,
            recipients=tuple((r.kind, r.id) for r in cmd.recipients),
        )
        run = GenerationRun(
            id=run_id,
            artefact_id=artefact.id,
            tenant_id=identity.tenant_id,
            principal_id=identity.principal_id,
            requested_mode=eligibility.requested_mode,
            effective_mode=eligibility.effective_mode,
            eligibility_state=eligibility.state,
            domain_profile_version=bundle.domain_profile.version,
            language_pack_version=bundle.language_pack.version,
            tender_id=artefact.tender_id,
            origin=ArtefactOrigin.REGENERATION,
            base_version_id=cmd.base_version_id,
            workflow_id=workflow_id,
            run_input=winput.to_dict(),
            clamped=eligibility.clamped,
            clamp_reason=eligibility.clamp_reason,
        )
        await self._write.insert_run(tx, run)
        await self._events.publish(
            tx,
            build_event(
                identity.tenant_id,
                tender_subject(artefact.tender_id),
                GenerationStarted(
                    artefact_id=str(artefact.id),
                    generation_id=str(run.id),
                    principal_id=str(identity.principal_id),
                    kind=artefact.kind,
                    capability=artefact.capability,
                    synchronous=False,
                    requested_mode=eligibility.requested_mode,
                    effective_mode=eligibility.effective_mode,
                    eligibility_state=eligibility.state,
                    clamped=eligibility.clamped,
                    domain_profile_version=run.domain_profile_version,
                    language_pack_version=run.language_pack_version,
                    tender_id=None if artefact.tender_id is None else str(artefact.tender_id),
                    target_ref=artefact.target_ref,
                    clamp_reason=eligibility.clamp_reason,
                    recipients=list(request.recipients),
                ),
                correlation_id=cmd.correlation_id,
            ),
        )
        return run


# --- A-6 -------------------------------------------------------------------------------------
def build_prompt(
    section_text: str,
    instruction: str | None,
    review_context: dict[str, object] | None,
    keep_fact: tuple[str, str] | None,
) -> str:
    """Layer 5 for a regeneration: the text being revised, the instruction, context, a kept fact.

    Everything here is content under the nonce boundary (GEN-4); none of it is stored (REGEN-3).
    """
    parts: list[str] = []
    if section_text:
        parts.append(f"Current section text:\n{section_text}")
    if instruction:
        parts.append(f"Reviewer instruction:\n{instruction}")
    if review_context:
        parts.append(
            "Review comments (context for this request only):\n"
            + json.dumps(review_context, ensure_ascii=False)
        )
    if keep_fact is not None:
        parts.append(f"Keep unchanged: {keep_fact[0]} = {keep_fact[1]}")
    return "\n\n".join(parts)


class RegenerateSection:
    """A-6 ``POST /api/v1/artefacts/{id}/sections/{ref}/regenerate`` (SSE, like R-3)."""

    def __init__(
        self,
        deps: RegenerationDeps,
        admit: AdmitGeneration,
        engine: ShortFormEngine,
        lifecycle: RunLifecycle,
        write: GenerationWriteRepository,
        facts: FactRepository,
        refinements: InstructionRefinementRepository,
        refiner: InstructionRefiner,
        store: ArtefactContentStore,
        events: EventPublisher,
        settings: GenerationSettings,
    ) -> None:
        self._cfg = settings
        self._d, self._admit, self._engine, self._lifecycle = deps, admit, engine, lifecycle
        self._write, self._facts, self._refinements = write, facts, refinements
        self._refiner, self._store, self._events = refiner, store, events

    async def execute(
        self,
        identity: RequestIdentity,
        cmd: RegenerateSectionCommand,
        *,
        is_disconnected: Callable[[], Awaitable[bool]] | None = None,
    ) -> GenerationStream:
        """Admit, refine, retrieve, stream. Every failure before byte one is an HTTP problem."""
        d, began = self._d, time.monotonic()
        artefact = await authorized_artefact(d, identity, cmd.artefact_id)
        rules.require_regenerable(artefact)
        found = await d.tx.run_in_tx(
            lambda tx: d.regen.get_section(tx, artefact.id, cmd.section_ref), read_only=True
        )
        if found is None:
            raise _not_found()
        section, current = found
        holds = False
        doc_id, section_version_id = (
            artefact.current_version_id,
            None if current is None else current.id,
        )
        if doc_id is not None and section_version_id is not None:
            holds = await d.tx.run_in_tx(
                lambda tx: d.regen.document_holds_section_version(tx, doc_id, section_version_id),
                read_only=True,
            )
        if artefact.current_version_id is None:
            # ck_run_base: a regeneration run names a base document version, and this artefact has
            # none (a failed or cancelled long-form run). A-6 cannot record the attempt, so refuse.
            raise rules.stale("The artefact has no document version yet.")
        rules.guard_section_base(
            artefact, section, cmd.base_version_id, document_holds_current_section_version=holds
        )  # REGEN-2
        prior_text = await self._prior_text(identity.tenant_id, current)
        instruction = (cmd.instruction or "").strip() or None
        facts: list[DocumentFact] = []
        if rules.long_form(artefact):  # REGEN-4: only a long-form document has established facts
            facts = await d.tx.run_in_tx(
                lambda tx: self._facts.current_facts(tx, artefact.id), read_only=True
            )
        request = AdmissionRequest(
            kind=artefact.kind,
            capability=artefact.capability,
            section_type=section.section_type,
            user_text=build_prompt(prior_text, instruction, cmd.review_context, None),
            scope=ScopeSpec(tender_id=artefact.tender_id),
            required_level=GrantLevel.GENERATE,
            requested_mode=GenerationMode.ASSISTANCE,
            profile_section=section.section_type,
            tender_id=artefact.tender_id,
            tender_section_ref=section.tender_section_ref,
            correlation_id=cmd.correlation_id,
            recipients=cmd.recipients,
        )
        try:
            prep = await self._admit.prepare(identity, request)
        except DomainError as exc:
            raise _admission_scope_error(exc) from exc
        run = await d.tx.run_in_tx(
            lambda tx: self._commit(
                tx, identity, artefact, section, current, cmd, prep.bundle, prep.eligibility
            )
        )
        adm = Admitted(
            identity,
            request,
            prep.scope,
            prep.bundle,
            tuple(prep.markers),
            prep.eligibility,
            prep.profile,
            artefact,
            run,
            section,
        )
        d.observer.regeneration(
            "section", "review" if cmd.review_context else "instruction" if instruction else "retry"
        )
        try:
            refined = await self._refine(adm, identity, section, instruction, prior_text, facts)
        except BaseException:
            await self._lifecycle.cancel_quietly(adm, "internal")
            raise
        conflict, keep, refine_run = refined
        if conflict is not None:  # REGEN-6: surfaced as a choice, nothing generated
            await self._lifecycle.cancel_quietly(adm, "fact_conflict")
            return GenerationStream.single(
                _conflict_frame(adm, conflict),
                adm.eligibility,
                self._cfg.heartbeat_seconds,
            )
        if refine_run is not None and refine_run.markers:
            adm = _with_markers(adm, refine_run.markers)
        text = (
            instruction
            if refine_run is None or not refine_run.result.usable
            else (refine_run.result.refined_instruction)
        )
        spec = ShortFormSpec(
            task="draft",
            kind=artefact.kind,
            capability=artefact.capability,
            section_type=section.section_type,
            profile_section=section.section_type,
            query=f"{section.section_type} {text or ''}".strip(),
            user_prompt=build_prompt(prior_text, text, cmd.review_context, keep),
            scope=request.scope,
            required_level=GrantLevel.GENERATE,
            admission=request,
            enforce_required=True,
            deadline=self._cfg.draft_deadline_seconds,
            metric_kind="draft",
            regeneration=SectionRegeneration(
                prior_section_version_id=None if current is None else current.id,
                manual_regen_count=(0 if current is None else current.manual_regen_count) + 1,
            ),
        )
        return await self._engine.run_admitted(
            adm, spec, began=began, is_disconnected=is_disconnected
        )

    # --- steps -----------------------------------------------------------------------------
    async def _prior_text(self, tenant_id: UUID, current: SectionVersion | None) -> str:
        if current is None:
            return ""
        raw = await self._store.get(tenant_id, current.content_ref)
        return raw.decode("utf-8", errors="replace")

    async def _commit(
        self,
        tx: TxHandle,
        identity: RequestIdentity,
        artefact: Artefact,
        section: ArtefactSection,
        current: SectionVersion | None,
        cmd: RegenerateSectionCommand,
        bundle: PackBundle,
        eligibility: EligibilityVerdict,
    ) -> GenerationRun:
        """One transaction: lock the section (two regenerations take turns), run row, event."""
        assert identity.principal_id is not None
        locked = await self._d.regen.lock_section(tx, section.id)
        if locked is None:
            raise _not_found()
        if locked.state in (SectionState.GENERATING, SectionState.PENDING):
            # §14.1 would make a concurrent regeneration a second version; the section machine has
            # no generating -> generating edge, so the second request is refused instead.
            raise rules.stale("The section is being generated.")
        run = GenerationRun(
            id=uuid4(),
            artefact_id=artefact.id,
            tenant_id=identity.tenant_id,
            principal_id=identity.principal_id,
            requested_mode=eligibility.requested_mode,
            effective_mode=eligibility.effective_mode,
            eligibility_state=eligibility.state,
            domain_profile_version=bundle.domain_profile.version,
            language_pack_version=bundle.language_pack.version,
            tender_id=artefact.tender_id,
            origin=ArtefactOrigin.REGENERATION,
            base_version_id=artefact.current_version_id or cmd.base_version_id,
            run_input={
                "synchronous": True,
                "kind": artefact.kind.value,
                "regeneration": "section",
                "section_id": str(section.id),
                "base_section_version_id": None if current is None else str(current.id),
            },
            clamped=eligibility.clamped,
            clamp_reason=eligibility.clamp_reason,
        )
        await self._write.insert_run(tx, run)
        await self._events.publish(
            tx,
            build_event(
                identity.tenant_id,
                tender_subject(artefact.tender_id),
                GenerationStarted(
                    artefact_id=str(artefact.id),
                    generation_id=str(run.id),
                    principal_id=str(identity.principal_id),
                    kind=artefact.kind,
                    capability=artefact.capability,
                    synchronous=True,
                    requested_mode=eligibility.requested_mode,
                    effective_mode=eligibility.effective_mode,
                    eligibility_state=eligibility.state,
                    clamped=eligibility.clamped,
                    domain_profile_version=run.domain_profile_version,
                    language_pack_version=run.language_pack_version,
                    tender_id=None if artefact.tender_id is None else str(artefact.tender_id),
                    target_ref=artefact.target_ref,
                    clamp_reason=eligibility.clamp_reason,
                    recipients=list(cmd.recipients),
                ),
                correlation_id=cmd.correlation_id,
            ),
        )
        return run

    async def _refine(
        self,
        adm: Admitted,
        identity: RequestIdentity,
        section: ArtefactSection,
        instruction: str | None,
        prior_text: str,
        facts: list[DocumentFact],
    ) -> tuple[FactConflict | None, tuple[str, str] | None, RefineRun | None]:
        """Run the refiner (flag-gated), record its row, and surface a conflict (REGEN-4/6).

        A conflict a reviewer already resolved ``keep_established`` is not raised again: the
        instruction is applied with the established value held fixed.
        """
        if instruction is None:
            return None, None, None
        d = self._d
        ctx = GatewayContext(identity.plan, identity.residency_mode.value)
        run = await self._refiner.refine(
            instruction=instruction,
            section_text=prior_text,
            section_type=section.section_type,
            mandatory_clauses=adm.profile.mandatory_clauses,
            facts=facts,
            bundle=adm.bundle,
            ctx=ctx,
        )
        if run is None:  # flag off: skipped entirely, no marker, no row (REF-2)
            return None, None, None
        finding = run.result.fact_conflict
        conflict: FactConflict | None = None
        keep: tuple[str, str] | None = None

        async def record(tx: TxHandle) -> None:
            nonlocal conflict, keep
            conflict_id: UUID | None = None
            if finding is not None:
                prior = await self._facts.find_resolved(
                    tx, section.id, finding.fact_key, finding.instruction_value
                )
                if prior is None:
                    conflict = FactConflict(
                        id=uuid4(),
                        tenant_id=adm.identity.tenant_id,
                        artefact_id=adm.artefact.id,
                        section_id=section.id,
                        fact_key=finding.fact_key,
                        established_value=finding.established_value,
                        instruction_value=finding.instruction_value,
                        established_by_section_id=finding.established_by_section_id,
                    )
                    await self._facts.insert_conflict(tx, conflict)
                    conflict_id = conflict.id
                elif prior.resolution is ConflictResolution.KEEP_ESTABLISHED:
                    keep = (prior.fact_key, prior.established_value)
                    conflict_id = prior.id
            await self._refinements.insert(
                tx,
                InstructionRefinement(
                    id=uuid4(),
                    tenant_id=adm.identity.tenant_id,
                    principal_hash=principal_hash(
                        adm.identity.tenant_id, adm.identity.principal_id
                    ),
                    original_instruction=instruction,
                    refined_instruction=run.result.refined_instruction
                    if run.result.usable
                    else None,
                    instruction_type=run.result.instruction_type,
                    needs_retrieval=run.result.needs_retrieval,
                    pack_version=run.pack_version,
                    refiner_version=run.result.refiner_version,
                    latency_ms=run.latency_ms,
                    outcome=run.result.outcome,
                    section_id=section.id,
                    fact_conflict_id=conflict_id,
                ),
            )

        await d.tx.run_in_tx(record)
        return conflict, keep, run


def _with_markers(adm: Admitted, extra: tuple[DegradationMarker, ...]) -> Admitted:
    from dataclasses import replace

    return replace(adm, pack_markers=(*adm.pack_markers, *extra))


def _conflict_frame(adm: Admitted, c: FactConflict) -> VerdictFrame:
    return VerdictFrame(
        artefact_id=adm.artefact.id,
        generation_id=adm.run.id,
        retrieval_set_id=None,
        evidence_tier=EvidenceTier.NOT_COMPUTED,
        eligibility=adm.eligibility,
        delivery=DeliveryState.NOT_APPLICABLE,
        degradation_markers=tuple(m.as_dict() for m in adm.pack_markers),
        fact_conflict=FactConflictOut(
            conflict_id=c.id,
            fact_key=c.fact_key,
            established_value=c.established_value,
            established_by_section_id=c.established_by_section_id,
            instruction_value=c.instruction_value,
        ),
    )


# --- A-7 -------------------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class ConflictResolved:
    """A-7's ``200`` body."""

    conflict_id: UUID
    resolution: ConflictResolution
    proceeds: bool
    """``keep_established`` and ``update_fact``: the client re-issues the regeneration (LD-26)."""
    flagged_section_ids: tuple[UUID, ...]


class ResolveFactConflict:
    """A-7 ``POST /api/v1/artefacts/{id}/sections/{ref}/fact-conflict-resolution``."""

    def __init__(
        self,
        deps: RegenerationDeps,
        facts: FactRepository,
        refinements: InstructionRefinementRepository,
        audit_log: AppendAuditEntry,
        gate: TenderGate,
    ) -> None:
        self._d, self._facts, self._refinements, self._audit = deps, facts, refinements, audit_log
        self._gate = gate

    async def execute(
        self,
        identity: RequestIdentity,
        artefact_id: UUID,
        section_ref: str,
        conflict_id: UUID,
        resolution: ConflictResolution,
    ) -> ConflictResolved:
        """Resolve once; regenerates nothing (REGEN-6, LD-32).

        ``update_fact`` supersedes the old fact, inserts the new and flags the document's other
        sections for review.
        """
        d = self._d
        artefact = await authorized_artefact(d, identity, artefact_id)
        artefact.require_writable()  # ART-7
        if artefact.tender_id is not None:
            await self._gate.require_open(artefact.tender_id)  # COL-14: the closure itself
        if not rules.long_form(artefact):  # REGEN-4: a draft has no conflicts
            raise DomainError(ErrorCode.FACT_CONFLICT_NOT_FOUND, "The conflict was not found.")
        assert identity.principal_id is not None
        principal = identity.principal_id

        async def work(tx: TxHandle) -> ConflictResolved:
            found = await d.regen.get_section(tx, artefact.id, section_ref)
            if found is None:
                raise _not_found()
            section = found[0]
            conflict = await self._facts.get_conflict(tx, conflict_id, lock=True)
            if (
                conflict is None
                or conflict.artefact_id != artefact.id
                or conflict.section_id != section.id
            ):
                raise DomainError(ErrorCode.FACT_CONFLICT_NOT_FOUND, "The conflict was not found.")
            now = d.clock.now()
            outcome = resolve_conflict(conflict, resolution, now)  # 409 where already resolved
            if not await self._facts.save_resolution(tx, conflict):
                raise DomainError(
                    ErrorCode.WORKFLOW_STATE_CONFLICT, "The conflict was resolved concurrently."
                )
            flagged = await self._apply(tx, artefact, conflict, outcome, now)
            await self._refinements.record_resolution(tx, conflict.id, resolution.value)
            await self._record(tx, artefact, conflict, resolution, flagged, principal, now)
            return ConflictResolved(conflict.id, resolution, outcome.proceeds, tuple(flagged))

        async with d.observer.span("regeneration.resolve_conflict"):
            result = await d.tx.run_in_tx(work)
        d.observer.fact_conflict(resolution.value)
        return result

    async def _apply(
        self,
        tx: TxHandle,
        artefact: Artefact,
        conflict: FactConflict,
        outcome: ConflictOutcome,
        now: datetime,
    ) -> list[UUID]:
        if not outcome.supersede_fact:
            return []
        await self._facts.supersede_fact(tx, artefact.id, conflict.fact_key, now)  # DF-2
        assert outcome.insert_value is not None
        await self._facts.insert_fact(
            tx,
            DocumentFact(
                id=uuid4(),
                artefact_id=artefact.id,
                tenant_id=artefact.tenant_id,
                fact_key=conflict.fact_key,
                value=outcome.insert_value,
                established_by_section_id=conflict.section_id,
            ),
        )
        # Which sections asserted the old value is not recorded (only the establishing section
        # is). Stage 2 generates the others with every fact as context (WP-16), so every other
        # section with a version may hold it: flag them all, regenerate none (REGEN-6).
        return await self._facts.flag_sections(
            tx,
            artefact.id,
            except_section_id=conflict.section_id,
            reason=f"fact_updated:{conflict.fact_key}",
        )

    async def _record(
        self,
        tx: TxHandle,
        artefact: Artefact,
        conflict: FactConflict,
        resolution: ConflictResolution,
        flagged: list[UUID],
        principal: UUID,
        now: datetime,
    ) -> None:
        await self._audit(
            tx,
            audit.AuditEntryDraft(
                tenant_id=artefact.tenant_id,
                occurred_at=now,
                entry_type=audit.FACT_CONFLICT_RESOLVED[0],
                action=audit.FACT_CONFLICT_RESOLVED[1],
                actor_type=AuditActorType.USER,
                actor_id=principal,
                artefact_id=artefact.id,
                source_event_type="FactConflictResolved",
                source_event_id=f"fact_conflict_resolved:{conflict.id}",
                target_type="fact_conflict",
                target_id=str(conflict.id),
                metadata={
                    "conflict_id": str(conflict.id),
                    "fact_key": conflict.fact_key,
                    "resolution": resolution.value,
                },
            ),
        )
        for section_id in flagged:
            await self._audit(
                tx,
                audit.AuditEntryDraft(
                    tenant_id=artefact.tenant_id,
                    occurred_at=now,
                    entry_type=audit.SECTION_FLAGGED[0],
                    action=audit.SECTION_FLAGGED[1],
                    actor_type=AuditActorType.USER,
                    actor_id=principal,
                    artefact_id=artefact.id,
                    source_event_type="SectionFlagged",
                    source_event_id=f"section_flagged:{conflict.id}:{section_id}",
                    target_type="section",
                    target_id=str(section_id),
                    metadata={
                        "section_id": str(section_id),
                        "flagged_reason": f"fact_updated:{conflict.fact_key}",
                    },
                ),
            )


__all__ = [
    "ConflictResolved",
    "RegenerateDocument",
    "RegenerateDocumentCommand",
    "RegenerateSection",
    "RegenerateSectionCommand",
    "RegenerationDeps",
    "ResolveFactConflict",
    "authorized_artefact",
]
