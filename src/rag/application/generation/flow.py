"""Admission, run lifecycle and completion: the building blocks every generation route shares.

R-2, R-3 and R-4 compose these; the workflows slice reuses them for A-6 and the section
activity (``AdmitGeneration``, ``RunLifecycle``, ``CompleteGeneration``).

Two writes per generation (LD-3): at **admission**, before any dispatch, the artefact shell,
its run row and its section commit together with the eligibility verdict, the clamp and the
pinned pack versions (RUN-1); at **completion** the version, its verification rows, the audit
entry and the outbox rows commit in one transaction (GEN-10).
"""

from __future__ import annotations

import copy
import dataclasses
import hashlib
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID, uuid4

from fastapicommon.logging import get_logger

from rag.application.events.contracts import (
    ExtractionCompleted,
    GenerationCompleted,
    GenerationStarted,
    Recipient,
    SectionCompleted,
    build_event,
    tender_subject,
)
from rag.application.generation.commands import AppendAuditEntry, MarkVersionsSuperseded
from rag.application.generation.ports import (
    ArtefactContentStore,
    StoreProbe,
    TenderGate,
)
from rag.application.packs.ports import PackResolver
from rag.application.packs.resolve import resolve_or_default
from rag.application.retrieval.scope import ScopeProvider, ScopeSpec
from rag.application.shared.events import EventPublisher
from rag.application.shared.gateway import GatewayContext, TokenCounter
from rag.application.shared.ports import TransactionRunner, TxHandle
from rag.application.verification.verify import VerificationResult
from rag.domain.generation import audit
from rag.domain.generation.artefact import Artefact, ArtefactVersion
from rag.domain.generation.eligibility import EligibilityVerdict, resolve_eligibility
from rag.domain.generation.repositories import (
    Clock,
    CompletionRows,
    GenerationWriteRepository,
    IdempotencyRace,
)
from rag.domain.generation.run import GenerationRun
from rag.domain.generation.section import ArtefactSection, SectionVersion
from rag.domain.generation.verification import (
    ClaimVerification,
    CompletenessCheck,
    GroundednessReport,
)
from rag.domain.packs.profile import PackBundle, SectionTypeProfile
from rag.domain.retrieval.budget import plan_budget
from rag.domain.retrieval.plan import DegradationMarker
from rag.domain.retrieval.scope import ResolvedScope
from rag.domain.shared.enums import (
    ArtefactKind,
    ArtefactOrigin,
    ArtefactStatus,
    AuditActorType,
    DeliveryState,
    GenerationMode,
    GrantLevel,
    RunFailureReason,
    RunStage,
    RunStatus,
    SectionState,
)
from rag.domain.shared.errors import DomainError, ErrorCode
from rag.domain.shared.identity import RequestIdentity
from rag.domain.workflows.ids import original_workflow_id
from rag.domain.workflows.input import ScopeSeed, WorkflowInput

log = get_logger(__name__)
ADMISSION_ATTEMPTS = 3
"""Re-reads after losing an ``Idempotency-Key`` race before the conflict is reported."""


@dataclass(frozen=True, slots=True)
class GenerationSettings:
    """Tunables of the generation path (LLD §17 plus design defaults, CFG-1)."""

    window_tokens: int = 128_000
    output_fraction: float = 0.25
    user_input_max_fraction: float = 0.10
    idempotency_ttl_seconds: int = 3600
    heartbeat_seconds: float = 15.0
    answer_deadline_seconds: float = 12.0
    """Gateway call budget: twice the 6 s verified-answer target (§14.3)."""
    draft_deadline_seconds: float = 40.0
    """Twice the 20 s interactive draft target."""
    extraction_deadline_seconds: float = 90.0
    """``EXTRACTION_SYNC_TIMEOUT_SECONDS``. Not doubled: the request timeout (120 s) caps it."""
    parser_version: str = "unrecorded"
    chunker_version: str = "unrecorded"
    """Not carried by ``ScopedCollectionInfo``; recorded as ``unrecorded`` until it is."""


@dataclass(frozen=True, slots=True)
class AdmissionRequest:
    """What admission needs of a request, whichever route it is (§5.6 order)."""

    kind: ArtefactKind
    capability: str
    section_type: str
    user_text: str
    scope: ScopeSpec
    required_level: GrantLevel
    requested_mode: GenerationMode = GenerationMode.ASSISTANCE
    profile_section: str | None = None
    """The section type that selects the profile row (R-3); ``None`` takes the default."""
    tender_id: UUID | None = None
    tender_section_ref: str | None = None
    idempotency_key: str | None = None
    request_hash: str | None = None
    stage: RunStage | None = None
    correlation_id: str = ""
    check_pack: Callable[[PackBundle], None] | None = None
    """A route's own pack check (R-4's ``schema_ref``), run after pack resolution, before commit."""
    target_ref: str | None = None
    outline_gate: bool = True
    """W-1: ``false`` skips the human gate where the pack permits it."""
    extras: Mapping[str, str] | None = None
    """W-6: ``schema_ref`` and other identifiers carried on the run input (never content)."""
    recipients: tuple[Recipient, ...] = ()
    """Echoed on the events, never inferred (LD-83)."""


@dataclass(frozen=True, slots=True)
class Admitted:
    """A committed shell and run, with everything resolved on the way (pinned for the run)."""

    identity: RequestIdentity
    request: AdmissionRequest
    scope: ResolvedScope
    bundle: PackBundle
    pack_markers: tuple[DegradationMarker, ...]
    eligibility: EligibilityVerdict
    profile: SectionTypeProfile
    artefact: Artefact
    run: GenerationRun
    section: ArtefactSection


@dataclass(frozen=True, slots=True)
class Replayed:
    """A repeat of a completed request inside the TTL: the stored artefact answers it."""

    artefact_id: UUID
    run: GenerationRun


@dataclass(frozen=True, slots=True)
class AsyncAdmitted:
    """A committed async shell and run (W-1, W-6): the workflow is not started yet (ASYNC-2).

    ``section`` is ``None`` for a document: its sections are the outline's rows, which the Worker
    Pool's outline activity writes (RUN-4). An extraction keeps its one section.
    """

    identity: RequestIdentity
    request: AdmissionRequest
    bundle: PackBundle
    eligibility: EligibilityVerdict
    artefact: Artefact
    run: GenerationRun
    section: ArtefactSection | None
    workflow_input: WorkflowInput


@dataclass(frozen=True, slots=True)
class AsyncReplayed:
    """The key already names this request's run.

    Its handle answers, and a run still ``pending`` has its start re-issued (the engine absorbs a
    repeat, ASYNC-2).
    """

    run: GenerationRun


def merge_markers(*groups: Iterable[Mapping[str, str]]) -> list[dict[str, str]]:
    """Degradation markers in order, each ``(stage, reason, applied_fallback)`` once.

    Admission and the retrieval pipeline both resolve packs, so a Tender outage would otherwise
    be reported twice on the same version.
    """
    seen: set[tuple[str, str, str]] = set()
    out: list[dict[str, str]] = []
    for group in groups:
        for m in group:
            key = (m["stage"], m["reason"], m["applied_fallback"])
            if key not in seen:
                seen.add(key)
                out.append(dict(m))
    return out


def _adopt[T](dst: T, src: T) -> None:
    """Copy ``src``'s fields onto ``dst``: a transaction's state changes apply only on commit."""
    for f in dataclasses.fields(dst):  # type: ignore[arg-type]
        setattr(dst, f.name, getattr(src, f.name))


def request_hash(*parts: object) -> str:
    """SHA-256 of the request's identifying fields (RUN-3): equal requests hash equally."""
    blob = "\x1f".join("" if p is None else str(p) for p in parts)
    return hashlib.sha256(blob.encode()).hexdigest()


def _idempotency_verdict(
    existing: GenerationRun, req_hash: str | None, now: datetime, ttl: int
) -> str:
    """``conflict``, ``in_flight``, ``replay`` or ``reuse`` for the run holding the key."""
    if existing.request_hash != req_hash:
        return "conflict"
    if existing.status in (RunStatus.FAILED, RunStatus.CANCELLED):
        return "reuse"  # nothing was produced: the key is released for a retry
    if existing.status is not RunStatus.COMPLETE:
        return "in_flight"
    created = existing.created_at
    expired = created is not None and (now - created).total_seconds() > ttl
    return "reuse" if expired else "replay"


def _async_verdict(existing: GenerationRun, req_hash: str | None, now: datetime, ttl: int) -> str:
    """``conflict``, ``replay`` or ``reuse`` for an async run holding the key (RUN-3).

    Unlike the synchronous verdict, a run in flight is a *replay*: a long-form run lives for
    days, and a client that lost the ``202`` must get its handle back, not a ``409`` (§5.7.12).
    """
    if existing.request_hash != req_hash:
        return "conflict"
    if existing.status in (RunStatus.FAILED, RunStatus.CANCELLED):
        return "reuse"
    created = existing.created_at
    expired = (
        existing.status is RunStatus.COMPLETE
        and created is not None
        and (now - created).total_seconds() > ttl
    )
    return "reuse" if expired else "replay"


@dataclass(frozen=True, slots=True)
class _Prepared:
    scope: ResolvedScope
    bundle: PackBundle
    markers: list[DegradationMarker]
    eligibility: EligibilityVerdict
    profile: SectionTypeProfile


class AdmitGeneration:
    """The admission checks in §5.6's order, then the shell and run commit (RUN-1, GEN-3).

    Tenant status, artefact-store writability, tender open (COL-14), scope, packs, eligibility,
    the user-layer budget, then one transaction: idempotency lookup, shell, run, section and the
    ``GenerationStarted`` outbox row. Returns ``Replayed`` where the key already holds a completed
    run inside ``IDEMPOTENCY_TTL_SECONDS``.
    """

    def __init__(
        self,
        tx: TransactionRunner,
        repo: GenerationWriteRepository,
        scope: ScopeProvider,
        packs: PackResolver,
        tokens: TokenCounter,
        probe: StoreProbe,
        gate: TenderGate,
        events: EventPublisher,
        clock: Clock,
        settings: GenerationSettings,
    ) -> None:
        self._tx, self._repo, self._scope, self._packs, self._tokens = (
            tx,
            repo,
            scope,
            packs,
            tokens,
        )
        self._probe, self._gate, self._events, self._clock = probe, gate, events, clock
        self._cfg = settings

    async def prepare(self, identity: RequestIdentity, req: AdmissionRequest) -> _Prepared:
        """§5.6's checks in order, up to the user-layer budget; nothing is written."""
        if identity.read_only:
            raise DomainError(ErrorCode.TENANT_NOT_ACTIVE, "The tenant does not admit generation.")
        if identity.principal_id is None:
            raise ValueError("generation needs a principal (API-1: identity comes from headers)")
        await self._probe.require_writable()  # GEN-3: before anything is dispatched or spent
        scope = await self._scope.resolve(identity, req.scope, req.required_level)
        if req.tender_id is not None:
            # After scope: a caller with no grant gets the uniform 404, not a 409 that would
            # confirm the tender exists and is closed (§6.2, OQ-19).
            await self._gate.require_open(req.tender_id)
        bundle, markers = await resolve_or_default(self._packs, identity.tenant_id)
        dp = bundle.domain_profile
        if req.check_pack is not None:
            req.check_pack(bundle)
        if dp.generation_model_profile and dp.generation_model_profile == dp.judge_model_profile:
            raise DomainError(  # VER-3: no model judges its own output under identical conditions
                ErrorCode.PACK_INVALID,
                f"Generation and judge Model Profiles are identical (pack {dp.version}).",
            )
        eligibility = resolve_eligibility(
            dp.eligibility_for(req.capability), req.requested_mode, dp.version
        )
        ctx = GatewayContext(
            identity.plan, identity.residency_mode.value, tenant_id=identity.tenant_id
        )
        used = (await self._tokens.count([req.user_text], ctx=ctx))[0]
        plan_budget(
            self._cfg.window_tokens,
            output_fraction=self._cfg.output_fraction,
            user_input_max_fraction=self._cfg.user_input_max_fraction,
            skeleton_tokens=dp.skeleton_tokens,
            user_input_tokens=used,
        )
        profile, _ = dp.profile_for(req.profile_section)
        return _Prepared(scope, bundle, markers, eligibility, profile)

    async def execute(
        self, identity: RequestIdentity, req: AdmissionRequest
    ) -> Admitted | Replayed:
        """Admit or replay. Raises the §5.6 problem of the first failing check."""
        prep = await self.prepare(identity, req)
        scope, bundle, markers = prep.scope, prep.bundle, prep.markers
        eligibility, profile = prep.eligibility, prep.profile
        for _attempt in range(ADMISSION_ATTEMPTS):
            try:
                found = await self._tx.run_in_tx(
                    lambda tx: self._commit(tx, identity, req, scope, bundle, markers, eligibility)
                )
            except IdempotencyRace:
                continue  # a concurrent admission won the key: re-read it
            if isinstance(found, GenerationRun):
                return Replayed(found.artefact_id, found)
            artefact, run, section = found
            return Admitted(
                identity,
                req,
                scope,
                bundle,
                tuple(markers),
                eligibility,
                profile,
                artefact,
                run,
                section,
            )
        raise DomainError(ErrorCode.IDEMPOTENCY_KEY_CONFLICT, "The Idempotency-Key is contended.")

    async def _commit(
        self,
        tx: TxHandle,
        identity: RequestIdentity,
        req: AdmissionRequest,
        scope: ResolvedScope,
        bundle: PackBundle,
        markers: Sequence[DegradationMarker],
        eligibility: EligibilityVerdict,
    ) -> GenerationRun | tuple[Artefact, GenerationRun, ArtefactSection]:
        assert identity.principal_id is not None
        key = req.idempotency_key
        if key is not None:
            existing = await self._repo.find_run_by_key(
                tx, identity.tenant_id, identity.principal_id, key
            )
            if existing is not None:
                verdict = _idempotency_verdict(
                    existing, req.request_hash, self._clock.now(), self._cfg.idempotency_ttl_seconds
                )
                if verdict == "conflict":
                    raise DomainError(
                        ErrorCode.IDEMPOTENCY_KEY_CONFLICT,
                        "The Idempotency-Key was used with a different request.",
                    )
                if verdict == "in_flight":
                    raise DomainError(
                        ErrorCode.IDEMPOTENCY_KEY_CONFLICT,
                        f"A run with this Idempotency-Key is in flight: {existing.id}.",
                    )
                if verdict == "replay":
                    return existing
                await self._repo.release_key(tx, existing.id)
        artefact = Artefact(
            id=uuid4(),
            tenant_id=identity.tenant_id,
            tender_id=req.tender_id,
            kind=req.kind,
            capability=req.capability,
            created_by=identity.principal_id,
            tender_section_ref=req.tender_section_ref,
        )
        section = ArtefactSection(
            id=uuid4(),
            artefact_id=artefact.id,
            tenant_id=identity.tenant_id,
            section_type=req.section_type,
            ordinal=1,
            tender_section_ref=req.tender_section_ref,
        )
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
            tender_id=req.tender_id,
            origin=ArtefactOrigin.ORIGINAL,
            run_input={"synchronous": True, "kind": req.kind.value},
            current_stage=req.stage,
            idempotency_key=key,
            request_hash=req.request_hash if key is not None else None,
            clamped=eligibility.clamped,
            clamp_reason=eligibility.clamp_reason,
        )
        await self._repo.insert_shell(tx, artefact, run, section)
        await self._events.publish(
            tx,
            build_event(
                identity.tenant_id,
                tender_subject(req.tender_id),
                GenerationStarted(
                    artefact_id=str(artefact.id),
                    generation_id=str(run.id),
                    principal_id=str(identity.principal_id),
                    kind=req.kind,
                    capability=req.capability,
                    synchronous=True,
                    requested_mode=eligibility.requested_mode,
                    effective_mode=eligibility.effective_mode,
                    eligibility_state=eligibility.state,
                    clamped=eligibility.clamped,
                    domain_profile_version=run.domain_profile_version,
                    language_pack_version=run.language_pack_version,
                    tender_id=None if req.tender_id is None else str(req.tender_id),
                    clamp_reason=eligibility.clamp_reason,
                ),
                correlation_id=req.correlation_id,
            ),
        )
        return artefact, run, section

    async def execute_async(
        self, identity: RequestIdentity, req: AdmissionRequest
    ) -> AsyncAdmitted | AsyncReplayed:
        """W-1 / W-6 admission: the §5.6 checks, then one transaction (ASYNC-2, RUN-1, RUN-2).

        Shell, run (``workflow_id`` = ``artefact_id``, ``run_input`` = the workflow's start
        argument) and ``GenerationStarted`` commit together. The caller starts the workflow after
        the commit; a failed start leaves this ``pending`` row for the orphan sweep.
        """
        prep = await self.prepare(identity, req)
        for _attempt in range(ADMISSION_ATTEMPTS):
            try:
                found = await self._tx.run_in_tx(
                    lambda tx: self._commit_async(tx, identity, req, prep)
                )
            except IdempotencyRace:
                continue
            if isinstance(found, GenerationRun):
                return AsyncReplayed(found)
            return found
        raise DomainError(ErrorCode.IDEMPOTENCY_KEY_CONFLICT, "The Idempotency-Key is contended.")

    async def _commit_async(
        self, tx: TxHandle, identity: RequestIdentity, req: AdmissionRequest, prep: _Prepared
    ) -> GenerationRun | AsyncAdmitted:
        assert identity.principal_id is not None
        key = req.idempotency_key
        if key is not None:
            existing = await self._repo.find_run_by_key(
                tx, identity.tenant_id, identity.principal_id, key
            )
            if existing is not None:
                verdict = _async_verdict(
                    existing, req.request_hash, self._clock.now(), self._cfg.idempotency_ttl_seconds
                )
                if verdict == "conflict":
                    raise DomainError(
                        ErrorCode.IDEMPOTENCY_KEY_CONFLICT,
                        "The Idempotency-Key was used with a different request.",
                    )
                if verdict == "replay":
                    return existing
                await self._repo.release_key(tx, existing.id)
        bundle, eligibility = prep.bundle, prep.eligibility
        artefact = Artefact(
            id=uuid4(),
            tenant_id=identity.tenant_id,
            tender_id=req.tender_id,
            kind=req.kind,
            capability=req.capability,
            created_by=identity.principal_id,
            tender_section_ref=req.tender_section_ref,
            target_ref=req.target_ref,
        )
        section = (
            ArtefactSection(
                id=uuid4(),
                artefact_id=artefact.id,
                tenant_id=identity.tenant_id,
                section_type=req.section_type,
                ordinal=1,
                tender_section_ref=req.tender_section_ref,
            )
            if req.kind is ArtefactKind.EXTRACTION
            else None
        )
        run_id = uuid4()
        winput = WorkflowInput(
            tenant_id=identity.tenant_id,
            principal_id=identity.principal_id,
            artefact_id=artefact.id,
            generation_run_id=run_id,
            capability=req.capability,
            requested_mode=eligibility.requested_mode.value,
            plan=identity.plan,
            residency_mode=identity.residency_mode.value,
            domain_profile_version=bundle.domain_profile.version,
            language_pack_version=bundle.language_pack.version,
            scope_seed=ScopeSeed(
                req.scope.tender_id, req.scope.collection_ids, req.scope.collection_types
            ),
            tender_id=req.tender_id,
            target_ref=req.target_ref,
            correlation_id=req.correlation_id,
            kind="extraction" if req.kind is ArtefactKind.EXTRACTION else "document",
            outline_gate=req.outline_gate or bundle.domain_profile.outline_gate_required,
            extras=dict(req.extras or {}),
            recipients=tuple((r.kind, r.id) for r in req.recipients),
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
            tender_id=req.tender_id,
            origin=ArtefactOrigin.ORIGINAL,
            workflow_id=original_workflow_id(artefact.id),
            run_input=winput.to_dict(),
            current_stage=req.stage,
            idempotency_key=key,
            request_hash=req.request_hash if key is not None else None,
            clamped=eligibility.clamped,
            clamp_reason=eligibility.clamp_reason,
        )
        await self._repo.insert_shell(tx, artefact, run, section)
        await self._events.publish(
            tx,
            build_event(
                identity.tenant_id,
                tender_subject(req.tender_id),
                GenerationStarted(
                    artefact_id=str(artefact.id),
                    generation_id=str(run.id),
                    principal_id=str(identity.principal_id),
                    kind=req.kind,
                    capability=req.capability,
                    synchronous=False,
                    requested_mode=eligibility.requested_mode,
                    effective_mode=eligibility.effective_mode,
                    eligibility_state=eligibility.state,
                    clamped=eligibility.clamped,
                    domain_profile_version=run.domain_profile_version,
                    language_pack_version=run.language_pack_version,
                    tender_id=None if req.tender_id is None else str(req.tender_id),
                    target_ref=req.target_ref,
                    clamp_reason=eligibility.clamp_reason,
                    recipients=list(req.recipients),
                ),
                correlation_id=req.correlation_id,
            ),
        )
        return AsyncAdmitted(identity, req, bundle, eligibility, artefact, run, section, winput)


class RunLifecycle:
    """Moves the artefact, run and section through their machines and persists the result.

    ``start`` runs right before the Gateway call (the run becomes ``generating``). Before
    dispatch the only way out is ``cancel`` (the run machine has no ``pending -> failed`` edge);
    after dispatch it is ``fail`` with a ``run_failure_reason``. Each closes in its own
    transaction and appends its audit entry in it (AC-1).
    """

    def __init__(
        self,
        tx: TransactionRunner,
        repo: GenerationWriteRepository,
        audit_log: AppendAuditEntry,
        clock: Clock,
    ) -> None:
        self._tx, self._repo, self._audit, self._clock = tx, repo, audit_log, clock

    async def start(self, a: Admitted) -> None:
        """``pending -> generating`` on the artefact, the run and its section."""

        async def work(tx: TxHandle) -> None:
            art, run, sec = copy.copy(a.artefact), copy.copy(a.run), copy.copy(a.section)
            if run.origin is ArtefactOrigin.ORIGINAL:  # a section regeneration leaves the artefact
                art.move_to(
                    ArtefactStatus.GENERATING
                )  # at rest: two sections may regenerate at once
            run.move_to(RunStatus.GENERATING)
            sec.start()
            await self._save(tx, art, run, sec)
            self._adopt_all(a, art, run, sec)

        await self._tx.run_in_tx(work)

    async def _save(
        self, tx: TxHandle, art: Artefact, run: GenerationRun, sec: ArtefactSection
    ) -> None:
        """A regeneration never writes the artefact row (it stays at rest, ART-5)."""
        if run.origin is ArtefactOrigin.ORIGINAL:
            await self._repo.save_state(tx, art, run, sec)
        else:
            await self._repo.save_run_and_section(tx, run, sec)

    @staticmethod
    def _adopt_all(a: Admitted, art: Artefact, run: GenerationRun, sec: ArtefactSection) -> None:
        _adopt(a.artefact, art)
        _adopt(a.run, run)
        _adopt(a.section, sec)

    async def cancel(self, a: Admitted, reason: str, *, gap_reason: str | None = None) -> None:
        """Close before dispatch as ``cancelled``; ``gap_reason`` also marks the section a gap."""

        async def work(tx: TxHandle) -> None:
            art, run, sec = copy.copy(a.artefact), copy.copy(a.run), copy.copy(a.section)
            run.move_to(RunStatus.CANCELLED)
            if run.origin is ArtefactOrigin.ORIGINAL:
                art.move_to(ArtefactStatus.CANCELLED)
                if gap_reason is not None:
                    sec.block(gap_reason)
            # A regeneration cancelled before dispatch touched neither: the artefact and the
            # section still describe their latest good output (AS-7); only the run records it.
            await self._save(tx, art, run, sec)
            await self._audit(tx, self._entry(a, audit.GENERATION_CANCELLED, reason))
            self._adopt_all(a, art, run, sec)

        await self._tx.run_in_tx(work)

    async def cancel_quietly(
        self, a: Admitted, reason: str, *, gap_reason: str | None = None
    ) -> None:
        """``cancel`` on an error path: a close that fails must not mask the error being raised.

        The stalled-generation sweep reaps a run this could not close (§21.2).
        """
        try:
            await self.cancel(a, reason, gap_reason=gap_reason)
        except Exception as exc:  # noqa: BLE001
            log.warning("run_close_failed", error=type(exc).__name__)

    async def fail(self, a: Admitted, reason: RunFailureReason) -> None:
        """Close after dispatch as ``failed`` with its reason (RUN-5, AS-5)."""

        async def work(tx: TxHandle) -> None:
            art, run, sec = copy.copy(a.artefact), copy.copy(a.run), copy.copy(a.section)
            # A regeneration that fails leaves the artefact and section at their latest good
            # output (AS-7); the run alone records the attempt.
            if run.origin is ArtefactOrigin.ORIGINAL:
                art.move_to(ArtefactStatus.FAILED)
            run.fail(reason, run.current_stage)
            sec.end_without_version(gap_reason=None, failure=reason)
            await self._save(tx, art, run, sec)
            await self._audit(tx, self._entry(a, audit.GENERATION_FAILED, reason.value))
            self._adopt_all(a, art, run, sec)

        await self._tx.run_in_tx(work)

    def _entry(self, a: Admitted, kind: tuple[str, str], reason: str) -> audit.AuditEntryDraft:
        event = "GenerationFailed" if kind is audit.GENERATION_FAILED else "GenerationCancelled"
        assert a.identity.principal_id is not None
        return audit.AuditEntryDraft(
            tenant_id=a.identity.tenant_id,
            occurred_at=self._clock.now(),
            entry_type=kind[0],
            action=kind[1],
            actor_type=AuditActorType.USER,
            actor_id=a.identity.principal_id,
            artefact_id=a.artefact.id,
            source_event_type=event,
            source_event_id=f"{event}:{a.run.id}",
            target_type="generation_run",
            target_id=str(a.run.id),
            metadata={"reason": reason},
        )


def verification_rows(
    tenant: UUID, section_version_id: UUID, v: VerificationResult
) -> tuple[GroundednessReport, tuple[ClaimVerification, ...], CompletenessCheck]:
    """The groundedness report, its claims and the completeness check of one section version."""
    report = GroundednessReport(
        id=uuid4(),
        section_version_id=section_version_id,
        tenant_id=tenant,
        tiers_run=v.tiers_run,
        claims_total=v.claims_total,
        supported=v.supported,
        unsupported=v.unsupported,
        miscited=v.miscited,
        gaps=v.gaps,
        evidence_tier=v.tier,
        score=v.score,
        judge_model_profile_id=v.judge_model_profile_id,
    )
    claims = tuple(
        ClaimVerification(
            report_id=report.id,
            claim_ordinal=c.ordinal,
            tenant_id=tenant,
            outcome=c.outcome,
            cited_chunk_ids=c.cited_chunk_ids,
            tier_decided=c.tier_decided,
            corrected_chunk_ids=c.corrected_chunk_ids or None,
        )
        for c in v.claims
    )
    check = CompletenessCheck(
        id=uuid4(),
        section_version_id=section_version_id,
        tenant_id=tenant,
        verdict=v.completeness.verdict,
        items_total=v.completeness.items_total,
        items_addressed=v.completeness.items_addressed,
        missed_items=v.completeness.missed_items,
    )
    return report, claims, check


@dataclass(frozen=True, slots=True)
class CompletionInput:
    """Everything the completion transaction writes (§7.5)."""

    admitted: Admitted
    retrieval_set_id: UUID
    content: bytes
    prompt_hash: str
    model_profile_id: str
    serving_tier: str
    verification: VerificationResult
    delivery: DeliveryState
    markers: tuple[dict[str, str], ...] = ()
    disclaimer_ref: str | None = None
    output_schema_version: str | None = None
    trigger_document_version_id: str | None = None
    """Set for an extraction: ``ExtractionCompleted`` is published too (LD-84)."""
    parser_version: str = "unrecorded"
    chunker_version: str = "unrecorded"


@dataclass(frozen=True, slots=True)
class Completed:
    """What the completion transaction minted."""

    version: ArtefactVersion
    section_version: SectionVersion


@dataclass(frozen=True, slots=True)
class SectionRegeneration:
    """What A-6's completion needs beyond ``CompletionInput``: the version it replaces."""

    prior_section_version_id: UUID | None
    manual_regen_count: int
    """Prior ``manual_regen_count`` + 1 (ASV)."""


@dataclass(frozen=True, slots=True)
class CompletedSection:
    """What a section regeneration minted: a section version and no document version (REGEN-8)."""

    section_version: SectionVersion
    prior_section_version_id: UUID | None
    superseded_version_ids: tuple[UUID, ...]


class CompleteGeneration:
    """Mints the version: content to S3, then one transaction for everything else (GEN-10).

    Under the per-artefact lock it allocates ``version_no`` (§14.1), writes the section version
    (``content_sha256`` over the bytes written, ASV-4), the document version, the groundedness
    and completeness rows, moves the three machines to ``complete``, appends the audit entry
    (``content_digest``, AC-8) and enqueues ``GenerationCompleted`` (and ``ExtractionCompleted``)
    in the same transaction. No approval state is written on any path (AOB-1).
    """

    def __init__(
        self,
        tx: TransactionRunner,
        repo: GenerationWriteRepository,
        store: ArtefactContentStore,
        audit_log: AppendAuditEntry,
        events: EventPublisher,
        clock: Clock,
        mark_superseded: MarkVersionsSuperseded | None = None,
    ) -> None:
        self._tx, self._repo, self._store = tx, repo, store
        self._audit, self._events, self._clock = audit_log, events, clock
        self._mark = mark_superseded

    async def execute_regeneration(
        self, inp: CompletionInput, regen: SectionRegeneration
    ) -> CompletedSection:
        """A-6's completion: one section version, no document version (REGEN-8), AV-6 flags.

        Same transaction shape as ``execute``: content to S3 first, then the version, its
        verification rows, the machines, the audit entry and ``SectionCompleted`` together.
        """
        try:
            return await self._tx.run_in_tx(lambda tx: self._write_section(tx, inp, regen))
        except DomainError:
            raise
        except Exception as exc:
            log.error("completion_failed", error=type(exc).__name__)  # never the message (OBS-2)
            raise DomainError(
                ErrorCode.ARTEFACT_STORE_UNWRITABLE, "The artefact could not be recorded."
            ) from exc

    async def _write_section(
        self, tx: TxHandle, inp: CompletionInput, regen: SectionRegeneration
    ) -> CompletedSection:
        assert self._mark is not None, "a regeneration needs MarkVersionsSuperseded (AV-6)"
        a, v = inp.admitted, inp.verification
        tenant, now = a.identity.tenant_id, self._clock.now()
        assert a.identity.principal_id is not None
        _, section_no = await self._repo.next_version_numbers(tx, a.artefact.id, a.section.id)
        key = self._store.section_key(tenant, a.artefact.id, a.section.id, section_no)
        digest = hashlib.sha256(inp.content).digest()  # ASV-4: over the bytes written
        await self._store.put(tenant, key, inp.content)
        markers = merge_markers((m.as_dict() for m in a.pack_markers), inp.markers, v.markers)
        sv = SectionVersion(
            id=uuid4(),
            section_id=a.section.id,
            tenant_id=tenant,
            version_no=section_no,
            content_ref=key,
            content_sha256=digest,
            retrieval_set_id=inp.retrieval_set_id,
            evidence_tier=v.tier,
            completeness=v.completeness.verdict,
            model_profile_id=inp.model_profile_id,
            prompt_hash=inp.prompt_hash,
            serving_tier=inp.serving_tier or "primary",
            origin=ArtefactOrigin.REGENERATION,
            manual_regen_count=regen.manual_regen_count,
            degradation_markers=markers,
        )
        report, claims, check = verification_rows(tenant, sv.id, v)
        await self._repo.insert_completion(tx, CompletionRows(None, sv, report, claims, check))
        superseded = await self._mark(tx, a.artefact.id, a.section.id)  # AV-6
        art, run, sec = copy.copy(a.artefact), copy.copy(a.run), copy.copy(a.section)
        sec.complete(sv.id)
        run.move_to(RunStatus.COMPLETE)  # the artefact is untouched: no document version minted
        await self._repo.save_run_and_section(tx, run, sec)
        await self._audit(
            tx,
            audit.AuditEntryDraft(
                tenant_id=tenant,
                occurred_at=now,
                entry_type=audit.SECTION_REGENERATED[0],
                action=audit.SECTION_REGENERATED[1],
                actor_type=AuditActorType.USER,
                actor_id=a.identity.principal_id,
                artefact_id=a.artefact.id,
                source_event_type="SectionCompleted",
                source_event_id=str(sv.id),
                target_type="section_version",
                target_id=str(sv.id),
                metadata={
                    "section_version_id": str(sv.id),
                    "content_sha256": digest.hex(),
                    "prompt_hash": inp.prompt_hash,
                    "retrieval_set_id": str(inp.retrieval_set_id),
                    "origin": ArtefactOrigin.REGENERATION.value,
                },
            ),
        )
        done, total = await self._repo.count_sections(tx, a.artefact.id)
        await self._events.publish(
            tx,
            build_event(
                tenant,
                tender_subject(a.artefact.tender_id),
                SectionCompleted(
                    artefact_id=str(a.artefact.id),
                    principal_id=str(a.identity.principal_id),
                    section_ref=a.section.tender_section_ref or str(a.section.id),
                    state=SectionState.COMPLETE,
                    origin=ArtefactOrigin.REGENERATION,
                    sections_complete=done,
                    sections_total=total,
                    generation_id=str(a.run.id),
                    tender_id=None if a.artefact.tender_id is None else str(a.artefact.tender_id),
                    section_version_id=str(sv.id),
                    evidence_tier=v.tier,
                    completeness=v.completeness.verdict,
                    retrieval_set_id=str(inp.retrieval_set_id),
                    superseded_version_ids=[str(i) for i in superseded],
                    recipients=list(a.request.recipients),
                ),
                correlation_id=a.request.correlation_id,
            ),
        )
        RunLifecycle._adopt_all(a, art, run, sec)
        return CompletedSection(sv, regen.prior_section_version_id, tuple(superseded))

    async def execute(self, inp: CompletionInput) -> Completed:
        """Write the version; ``503 ARTEFACT_STORE_UNWRITABLE`` where S3 or the store fails."""
        try:
            return await self._tx.run_in_tx(lambda tx: self._write(tx, inp))
        except DomainError:
            raise
        except Exception as exc:
            log.error("completion_failed", error=type(exc).__name__)  # never the message (OBS-2)
            raise DomainError(
                ErrorCode.ARTEFACT_STORE_UNWRITABLE, "The artefact could not be recorded."
            ) from exc

    async def _write(self, tx: TxHandle, inp: CompletionInput) -> Completed:
        a, v = inp.admitted, inp.verification
        tenant, now = a.identity.tenant_id, self._clock.now()
        assert a.identity.principal_id is not None
        version_no, section_no = await self._repo.next_version_numbers(
            tx, a.artefact.id, a.section.id
        )
        key = self._store.section_key(tenant, a.artefact.id, a.section.id, section_no)
        digest = hashlib.sha256(inp.content).digest()  # ASV-4: over the bytes written
        await self._store.put(tenant, key, inp.content)
        markers = merge_markers((m.as_dict() for m in a.pack_markers), inp.markers, v.markers)
        sv = SectionVersion(
            id=uuid4(),
            section_id=a.section.id,
            tenant_id=tenant,
            version_no=section_no,
            content_ref=key,
            content_sha256=digest,
            retrieval_set_id=inp.retrieval_set_id,
            evidence_tier=v.tier,
            completeness=v.completeness.verdict,
            model_profile_id=inp.model_profile_id,
            prompt_hash=inp.prompt_hash,
            serving_tier=inp.serving_tier or "primary",
            degradation_markers=markers,
        )
        av = ArtefactVersion(
            id=uuid4(),
            artefact_id=a.artefact.id,
            tenant_id=tenant,
            version_no=version_no,
            minted_reason="initial",
            evidence_tier=v.tier,
            completeness=v.completeness.verdict,
            eligibility_state=a.eligibility.state,
            requested_mode=a.eligibility.requested_mode,
            effective_mode=a.eligibility.effective_mode,
            domain_profile_version=a.run.domain_profile_version,
            language_pack_version=a.run.language_pack_version,
            model_profile_id=inp.model_profile_id,
            prompt_hash=inp.prompt_hash,
            parser_version=inp.parser_version,
            chunker_version=inp.chunker_version,
            generation_run_id=a.run.id,
            delivery=inp.delivery,
            clamped=a.eligibility.clamped,
            clamp_reason=a.eligibility.clamp_reason,
            output_schema_version=inp.output_schema_version,
            degradation_markers=markers,
            disclaimer_ref=inp.disclaimer_ref,
        )
        report, claims, check = verification_rows(tenant, sv.id, v)
        await self._repo.insert_completion(tx, CompletionRows(av, sv, report, claims, check))
        art, run, sec = copy.copy(a.artefact), copy.copy(a.run), copy.copy(a.section)
        sec.complete(sv.id)
        art.current_version_id = av.id
        art.move_to(ArtefactStatus.COMPLETE)
        run.move_to(RunStatus.COMPLETE)
        await self._repo.save_state(tx, art, run, sec)
        await self._audit(
            tx,
            audit.AuditEntryDraft(
                tenant_id=tenant,
                occurred_at=now,
                entry_type=audit.GENERATION_COMPLETED[0],
                action=audit.GENERATION_COMPLETED[1],
                actor_type=AuditActorType.USER,
                actor_id=a.identity.principal_id,
                artefact_id=a.artefact.id,
                source_event_type="GenerationCompleted",
                source_event_id=str(av.id),
                target_type="artefact_version",
                target_id=str(av.id),
                metadata={
                    "version_id": str(av.id),
                    "generation_run_id": str(a.run.id),
                    "prompt_hash": inp.prompt_hash,
                    "model_profile_id": inp.model_profile_id,
                    "domain_profile_version": av.domain_profile_version,
                    "language_pack_version": av.language_pack_version,
                    "evidence_tier": v.tier.value,
                    "completeness": v.completeness.verdict.value,
                    "retrieval_set_id": str(inp.retrieval_set_id),
                    "content_digest": audit.content_digest([(sv.id, digest)]),
                    "section_count": 1,
                },
            ),
        )
        subject = tender_subject(a.artefact.tender_id)
        corr = a.request.correlation_id
        tender = None if a.artefact.tender_id is None else str(a.artefact.tender_id)
        await self._events.publish(
            tx,
            build_event(
                tenant,
                subject,
                GenerationCompleted(
                    artefact_id=str(a.artefact.id),
                    artefact_version_id=str(av.id),
                    version_no=version_no,
                    principal_id=str(a.identity.principal_id),
                    kind=a.artefact.kind,
                    minted_reason=av.minted_reason,
                    origin=ArtefactOrigin.ORIGINAL,
                    evidence_tier=v.tier,
                    completeness=v.completeness.verdict,
                    delivery=inp.delivery,
                    section_count=1,
                    generation_id=str(a.run.id),
                    tender_id=tender,
                ),
                correlation_id=corr,
            ),
        )
        if inp.trigger_document_version_id is not None:
            await self._events.publish(
                tx,
                build_event(
                    tenant,
                    subject,
                    ExtractionCompleted(
                        artefact_id=str(a.artefact.id),
                        artefact_version_id=str(av.id),
                        generation_id=str(a.run.id),
                        principal_id=str(a.identity.principal_id),
                        trigger_document_version_id=inp.trigger_document_version_id,
                        schema_version=inp.output_schema_version or "",
                        evidence_tier=v.tier,
                        tender_id=tender,
                    ),
                    correlation_id=corr,
                ),
            )
        RunLifecycle._adopt_all(a, art, run, sec)
        return Completed(av, sv)
