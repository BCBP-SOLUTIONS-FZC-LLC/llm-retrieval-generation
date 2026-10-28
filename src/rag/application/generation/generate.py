"""R-2 and R-3: grounded answer and interactive draft, two-phase streaming (LLD §5.6, §7.4, §7.5).

``start`` does everything that can still be an ordinary HTTP problem (API-6): admission,
retrieval, input screening and the dispatch itself. It returns a ``GenerationStream`` only
once the first byte of the response is decided (the first draft frame, or on the blocking
compliance path the first heartbeat). A **producer task** then owns the generation: it drafts,
verifies, writes the artefact and puts frames on a queue. The response body only drains that
queue, so a client that drops (LD-18) cannot stop the generation or the artefact write
(GEN-2); the producer finishes and records ``delivery = disconnected``. The producer binds the
tenant itself (LD-23): nothing in it reads request-scoped state.
"""

from __future__ import annotations

import asyncio
import re
import time
from collections.abc import AsyncGenerator, Awaitable, Callable
from dataclasses import dataclass, replace
from uuid import UUID

from fastapicommon.logging import get_logger

from rag.application.generation.flow import (
    AdmissionRequest,
    AdmitGeneration,
    Admitted,
    CompleteGeneration,
    CompletionInput,
    GenerationSettings,
    Replayed,
    RunLifecycle,
    SectionRegeneration,
    merge_markers,
    request_hash,
)
from rag.application.generation.frames import (
    Citation,
    CompletenessOut,
    DraftFrame,
    ErrorFrame,
    Frame,
    GroundednessOut,
    Heartbeat,
    VerdictFrame,
)
from rag.application.generation.ports import (
    TenantBinder,
    UserContext,
    UserContextProvider,
    UserContextUnavailable,
)
from rag.application.generation.requests import AnswerCommand, DraftCommand
from rag.application.retrieval.dtos import PipelineRequest, RetrievalOutcome
from rag.application.retrieval.pipeline import RetrievalPipeline
from rag.application.retrieval.record import RecordRetrievalSet
from rag.application.retrieval.scope import ScopeSpec
from rag.application.shared.gateway import (
    TASK_DRAFT,
    GatewayContext,
    GatewayError,
    GatewayQuotaRefused,
    GatewayResidencyRefused,
    ModelGateway,
    StreamChunk,
    TokenCounter,
)
from rag.application.shared.observability import GenerationObserver, NullGenerationObserver
from rag.application.shared.ports import TransactionRunner
from rag.application.verification.gateway import GatewayEntailer, GatewayJudge, GatewayResampler
from rag.application.verification.verify import (
    VerificationRequest,
    VerificationResult,
    VerifySection,
)
from rag.domain.generation.eligibility import EligibilityVerdict
from rag.domain.generation.prompt import (
    ComposedPrompt,
    PromptChunk,
    compose_prompt,
    layer_texts,
    screen_layers,
)
from rag.domain.generation.repositories import ArtefactReadRepository
from rag.domain.retrieval.budget import plan_budget
from rag.domain.retrieval.candidate import Candidate
from rag.domain.shared.enums import (
    ArtefactKind,
    DeliveryState,
    EvidenceTier,
    GrantLevel,
    RetrievalVerdict,
    RunFailureReason,
)
from rag.domain.shared.errors import DomainError, ErrorCode
from rag.domain.shared.identity import RequestIdentity
from rag.domain.verification.tiers import EvidenceChunk

log = get_logger(__name__)
_MARKER = re.compile(r"\[(c-\d+(?:\s*,\s*c-\d+)*)\]")
ANSWER_CAPABILITY = "answer"


@dataclass(frozen=True, slots=True)
class ShortFormSpec:
    """One short-form request, whichever route it came from."""

    task: str  # "answer" | "draft": selects the layer 0 text
    kind: ArtefactKind
    capability: str
    section_type: str
    profile_section: str | None
    query: str
    user_prompt: str
    scope: ScopeSpec
    required_level: GrantLevel
    admission: AdmissionRequest
    enforce_required: bool
    deadline: float
    metric_kind: str
    regeneration: SectionRegeneration | None = None
    """A-6: complete a section version (no document version) and mark the prior one."""


def _refs(text: str) -> set[str]:
    return {r.strip() for m in _MARKER.finditer(text) for r in m[1].split(",")}


def marker(stage: str, reason: str, fallback: str) -> dict[str, str]:
    """A §5.5 degradation marker as the wire dict."""
    return {"stage": stage, "reason": reason, "applied_fallback": fallback}


def failure_for(exc: BaseException) -> tuple[RunFailureReason, DomainError | None]:
    """The run's ``failure_reason`` and the HTTP problem a failure before the first byte is.

    ``None`` for the problem means an unclassified failure: the platform's 500 answers it.
    """
    if isinstance(exc, GatewayQuotaRefused):
        return RunFailureReason.QUOTA_EXHAUSTED, DomainError(
            ErrorCode.QUOTA_EXHAUSTED,
            "The tenant's token quota is exhausted.",
            retry_after_seconds=exc.retry_after_seconds,
        )
    if isinstance(exc, GatewayResidencyRefused):
        return RunFailureReason.RESIDENCY_CAPACITY, DomainError(
            ErrorCode.RESIDENCY_CAPACITY, "No in-region inference capacity is available."
        )
    if isinstance(exc, GatewayError):
        return RunFailureReason.INFERENCE_UNAVAILABLE, DomainError(
            ErrorCode.INFERENCE_UNAVAILABLE, "Inference is unavailable."
        )
    if isinstance(exc, DomainError):
        reason = {
            ErrorCode.INFERENCE_UNAVAILABLE: RunFailureReason.INFERENCE_UNAVAILABLE,
            ErrorCode.ARTEFACT_STORE_UNWRITABLE: RunFailureReason.ARTEFACT_STORE_UNWRITABLE,
        }.get(exc.code, RunFailureReason.INTERNAL)
        return reason, exc
    return RunFailureReason.INTERNAL, None


def frame_error(exc: BaseException) -> ErrorFrame:
    """A failure after the first frame: ``INFERENCE_UNAVAILABLE`` or ``INTERNAL_ERROR`` only."""
    unavailable = isinstance(exc, GatewayError) or (
        isinstance(exc, DomainError) and exc.code is ErrorCode.INFERENCE_UNAVAILABLE
    )
    if unavailable:
        return ErrorFrame(ErrorCode.INFERENCE_UNAVAILABLE.value, "Inference is unavailable.")
    return ErrorFrame("INTERNAL_ERROR", "The generation could not be completed.")


def build_verdict(
    a: Admitted,
    result: VerificationResult,
    *,
    retrieval_set_id: UUID,
    version_id: UUID | None,
    version_no: int | None,
    delivery: DeliveryState,
    markers: tuple[dict[str, str], ...],
    disclaimer_ref: str | None,
) -> VerdictFrame:
    """The terminal frame: per-claim outcomes with corrections, verdicts, tier (§5.6)."""
    return VerdictFrame(
        artefact_id=a.artefact.id,
        generation_id=a.run.id,
        artefact_version_id=version_id,
        version_no=version_no,
        retrieval_set_id=retrieval_set_id,
        citations=tuple(
            Citation(c.ordinal, c.outcome, c.cited_chunk_ids, c.corrected_chunk_ids, c.tier_decided)
            for c in result.claims
        ),
        groundedness=GroundednessOut(
            result.supported, result.unsupported, result.miscited, result.gaps, result.tiers_run
        ),
        completeness=CompletenessOut(result.completeness.verdict, result.completeness.missed_items),
        evidence_tier=result.tier,
        eligibility=a.eligibility,
        degradation_markers=markers,
        delivery=delivery,
        disclaimer_ref=disclaimer_ref,
    )


class GenerationStream:
    """What the route streams: frames from the producer's queue, heartbeats while it is quiet.

    Closing ``events()`` before the sentinel arrived is a client disconnect: the producer is
    told, and still finishes (LD-18).
    """

    def __init__(
        self,
        queue: asyncio.Queue[Frame | None],
        eligibility: EligibilityVerdict,
        *,
        heartbeat_seconds: float,
        suppressed: bool = False,
        on_close: _ProducerHandle | None = None,
    ) -> None:
        self._queue, self._hb = queue, heartbeat_seconds
        self.eligibility, self.suppressed = eligibility, suppressed
        self._producer = on_close

    @classmethod
    def single(
        cls, frame: Frame, eligibility: EligibilityVerdict, heartbeat_seconds: float
    ) -> GenerationStream:
        """A stream of one verdict frame: a replay, or a refusal that dispatched nothing."""
        queue: asyncio.Queue[Frame | None] = asyncio.Queue()
        queue.put_nowait(frame)
        queue.put_nowait(None)
        return cls(queue, eligibility, heartbeat_seconds=heartbeat_seconds, suppressed=True)

    async def events(self) -> AsyncGenerator[Frame, None]:
        """Frames in order, ``Heartbeat`` whenever ``heartbeat_seconds`` pass in silence."""
        finished = False
        try:
            while True:
                try:
                    item = await asyncio.wait_for(self._queue.get(), self._hb)
                except TimeoutError:
                    yield Heartbeat()
                    continue
                if item is None:
                    finished = True
                    return
                yield item
        finally:
            if self._producer is not None:
                self._producer.stream_closed(delivered=finished)


class _ProducerHandle:
    """The producer as the stream sees it: told when the body ends, one way or the other."""

    def __init__(self, observer: GenerationObserver) -> None:
        self.disconnected = False
        self._obs = observer

    def stream_closed(self, *, delivered: bool) -> None:
        """The body generator ended: ``delivered`` is true only if the sentinel was read."""
        if not delivered:
            self.disconnected = True
        self._obs.verdict_frame(delivered)


class ShortFormEngine:
    """Shared engine of R-2 and R-3 (and, with its parts, A-6 later)."""

    def __init__(
        self,
        *,
        admit: AdmitGeneration,
        lifecycle: RunLifecycle,
        complete: CompleteGeneration,
        pipeline: RetrievalPipeline,
        recorder: RecordRetrievalSet,
        gateway: ModelGateway,
        verify: VerifySection,
        reads: ArtefactReadRepository,
        tx: TransactionRunner,
        binder: TenantBinder,
        settings: GenerationSettings,
        user_context: UserContextProvider | None = None,
        observer: GenerationObserver | None = None,
        tokens: TokenCounter | None = None,
    ) -> None:
        self._tokens = tokens
        self._admit, self._lifecycle, self._complete = admit, lifecycle, complete
        self._pipeline, self._recorder, self._gw, self._verify = pipeline, recorder, gateway, verify
        self._reads, self._tx, self._binder, self._cfg = reads, tx, binder, settings
        self._user_context = user_context
        self._obs = observer or NullGenerationObserver()
        self._tasks: set[asyncio.Task[None]] = set()  # strong refs: a producer must outlive GC

    async def drain(self, timeout: float) -> None:
        """Wait for every producer still running, up to ``timeout`` (graceful shutdown, GEN-2).

        A client that dropped leaves a producer tied to no request; shutdown must let it write
        its artefact before the Gateway client and the pools close.
        """
        if self._tasks:
            await asyncio.wait(set(self._tasks), timeout=timeout)

    async def start(
        self,
        identity: RequestIdentity,
        spec: ShortFormSpec,
        *,
        is_disconnected: Callable[[], Awaitable[bool]] | None = None,
    ) -> GenerationStream:
        """Admit, retrieve, screen, dispatch. Raises the §24 problem of anything before byte one.

        ``is_disconnected`` probes the client's connection just before the delivery state is
        chosen: a drop during a silent stage (the blocking path, verification) raises nothing
        until the next write, so the probe is what notices it.
        """
        began = time.monotonic()
        async with self._obs.span("generation.admit"):
            adm = await self._admit.execute(identity, spec.admission)
        if isinstance(adm, Replayed):
            return await self._replay(adm)
        return await self.run_admitted(adm, spec, began=began, is_disconnected=is_disconnected)

    async def run_admitted(
        self,
        adm: Admitted,
        spec: ShortFormSpec,
        *,
        began: float,
        is_disconnected: Callable[[], Awaitable[bool]] | None = None,
    ) -> GenerationStream:
        """Everything after admission: retrieve, screen, dispatch, then the producer takes over.

        R-2 and R-3 reach it through ``start``; A-6 admits against an existing artefact and calls
        it directly, with ``spec.regeneration`` set so the producer completes a section version.
        """
        try:
            outcome, set_id = await self._retrieve(adm, spec)
            if outcome.verdict is not RetrievalVerdict.SUFFICIENT:
                return await self._refuse(adm, outcome, set_id)
            plan = await self._plan(adm, spec, outcome)
        except asyncio.CancelledError:
            await asyncio.shield(self._lifecycle.cancel_quietly(adm, "client_cancelled"))
            raise
        except DomainError as exc:
            gap = "required_input_missing" if exc.code is ErrorCode.REQUIRED_INPUT_MISSING else None
            await self._lifecycle.cancel_quietly(adm, exc.code.value.lower(), gap_reason=gap)
            raise
        except Exception:
            await self._lifecycle.cancel_quietly(adm, "internal")
            raise
        producer = _Producer(self, adm, spec, plan, set_id, began, is_disconnected)
        task = asyncio.create_task(producer.run())
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        await producer.first_byte()
        return GenerationStream(
            producer.queue,
            adm.eligibility,
            heartbeat_seconds=self._cfg.heartbeat_seconds,
            suppressed=adm.profile.compliance,
            on_close=producer.handle,
        )

    # --- before the first byte -----------------------------------------------------------

    async def _retrieve(self, a: Admitted, spec: ShortFormSpec) -> tuple[RetrievalOutcome, UUID]:
        dp = a.bundle.domain_profile
        async with self._obs.span("generation.retrieve"):
            outcome = await self._pipeline.run(
                a.identity,
                PipelineRequest(
                    query=spec.query,
                    scope=spec.scope,
                    section_type=spec.profile_section,
                    required_level=spec.required_level,
                    pinned_pack_version=None if a.bundle.is_platform_default else dp.version,
                    enforce_required_inputs=spec.enforce_required,
                ),
            )
            recorded = await self._recorder.record(
                a.identity, outcome, tender_id=a.artefact.tender_id, artefact_id=a.artefact.id
            )
        return outcome, recorded.retrieval_set_id

    async def _refuse(
        self, a: Admitted, outcome: RetrievalOutcome, set_id: UUID
    ) -> GenerationStream:
        """Insufficient evidence: nothing is dispatched (no tokens), the verdict says why.

        The run closes ``cancelled``: no run status fits a content refusal, and ``failed``
        would put a caller outcome in the failure counters (§24, ``Insufficient`` is a verdict).
        """
        await self._lifecycle.cancel_quietly(a, "insufficient_evidence")
        markers = tuple(merge_markers(m.as_dict() for m in (*a.pack_markers, *outcome.markers)))
        frame = VerdictFrame(
            artefact_id=a.artefact.id,
            generation_id=a.run.id,
            retrieval_set_id=set_id,
            evidence_tier=EvidenceTier.NOT_COMPUTED,
            eligibility=a.eligibility,
            delivery=DeliveryState.NOT_APPLICABLE,
            degradation_markers=markers,
            refusal={
                "reason": "insufficient_evidence",
                "detail": outcome.verdict_reason or "insufficient",
            },
        )
        return GenerationStream.single(frame, a.eligibility, self._cfg.heartbeat_seconds)

    async def _replay(self, rep: Replayed) -> GenerationStream:
        """The stored artefact as one verdict frame, no phase 1 (§5.7.2)."""
        self._obs.idempotent_replay()

        async def read(tx: object) -> VerdictFrame:
            view = await self._reads.get_artefact_view(tx, rep.artefact_id)
            version = view.current_version if view else None
            if view is None or version is None:
                raise DomainError(ErrorCode.ARTEFACT_NOT_FOUND, "The stored artefact is missing.")
            detail = await self._reads.get_version_detail(tx, rep.artefact_id, version.version_no)
            sec = detail.sections[0] if detail and detail.sections else None
            gr = sec.groundedness if sec else None
            cc = sec.completeness if sec else None
            return VerdictFrame(
                artefact_id=rep.artefact_id,
                generation_id=rep.run.id,
                artefact_version_id=version.id,
                version_no=version.version_no,
                retrieval_set_id=sec.section_version.retrieval_set_id if sec else None,
                citations=tuple(
                    Citation(
                        c.claim_ordinal,
                        c.outcome,
                        c.cited_chunk_ids,
                        c.corrected_chunk_ids or (),
                        c.tier_decided,
                    )
                    for c in (sec.claims if sec else ())
                ),
                groundedness=GroundednessOut(
                    gr.supported, gr.unsupported, gr.miscited, gr.gaps, gr.tiers_run
                )
                if gr
                else GroundednessOut(),
                completeness=CompletenessOut(cc.verdict, cc.missed_items)
                if cc
                else CompletenessOut(),
                evidence_tier=version.evidence_tier,
                eligibility=EligibilityVerdict(
                    version.eligibility_state,
                    version.requested_mode,
                    version.effective_mode,
                    version.clamped,
                    version.domain_profile_version,
                    version.clamp_reason,
                ),
                degradation_markers=tuple(version.degradation_markers),
                delivery=version.delivery,
                disclaimer_ref=version.disclaimer_ref,
                replay=True,
            )

        frame = await self._tx.run_in_tx(read, read_only=True)
        return GenerationStream.single(frame, frame.eligibility, self._cfg.heartbeat_seconds)

    async def _plan(self, a: Admitted, spec: ShortFormSpec, outcome: RetrievalOutcome) -> _Plan:
        """Layers, input screening and the model hint: after retrieval, before dispatch (§15.3)."""
        dp = a.bundle.domain_profile
        markers = [
            marker(m.stage, m.reason, m.applied_fallback)
            for m in (*a.pack_markers, *outcome.markers)
        ]
        user_ctx: UserContext | None = None
        if self._user_context is not None:
            try:
                user_ctx = await self._user_context.get(a.identity)
            except UserContextUnavailable:  # layer 3 is steering, not evidence (GEN-5)
                markers.append(marker("user_context", "llm_context_unavailable", "layer_3_omitted"))
        if self._tokens is not None and user_ctx is not None:
            # §13.1: layer 3 and layer 5 share the user allocation; over it is 413, never trimmed
            counts = await self._tokens.count(
                [spec.user_prompt, user_ctx.text],
                ctx=GatewayContext(
                    a.identity.plan, a.identity.residency_mode.value, tenant_id=a.identity.tenant_id
                ),
            )
            plan_budget(
                self._cfg.window_tokens,
                output_fraction=self._cfg.output_fraction,
                user_input_max_fraction=self._cfg.user_input_max_fraction,
                skeleton_tokens=dp.skeleton_tokens,
                user_input_tokens=sum(counts),
            )
        chunks = [_prompt_chunk(c) for c in outcome.chunks]
        skeleton = a.profile.skeleton or (
            dp.answer_skeleton if spec.profile_section is None else ""
        )
        screen_layers(
            layer_texts(
                tenant_overrides=dp.tenant_overrides,
                user_context=user_ctx.text if user_ctx else "",
                chunks=chunks,
                user_prompt=spec.user_prompt,
            ),
            dp.content_rules,
        )
        composed = compose_prompt(
            task=spec.task,
            skeleton=skeleton,
            tenant_overrides=dp.tenant_overrides,
            user_context=user_ctx.text if user_ctx else "",
            chunks=chunks,
            user_prompt=spec.user_prompt,
        )
        for layer in composed.override_attempts:  # GEN-4: conflicts resolve upward, recorded
            markers.append(marker("prompt", "content_override_ignored", f"layer_{layer}"))
        ctx = GatewayContext(
            a.identity.plan,
            a.identity.residency_mode.value,
            user_ctx.preferred_model if user_ctx else None,
            a.identity.tenant_id,
        )
        evidence = [EvidenceChunk(c.chunk_id, c.text) for c in outcome.chunks]
        return _Plan(composed, ctx, evidence, tuple(merge_markers(markers)))


def _prompt_chunk(c: Candidate) -> PromptChunk:
    return PromptChunk(
        chunk_id=c.chunk_id,
        text=c.text,
        document_title=c.document_title,
        heading_path=c.heading_path,
        clause_ref=c.clause_ref,
        document_role=c.document_role,
        document_id=c.document_id,
        document_version=c.document_version,
        language=c.language,
        excerpt=c.excerpt,
    )


@dataclass(frozen=True, slots=True)
class _Plan:
    composed: ComposedPrompt
    ctx: GatewayContext
    evidence: list[EvidenceChunk]
    markers: tuple[dict[str, str], ...]


class _Producer:
    """Drafts, verifies and records one generation, independent of the client's connection."""

    def __init__(
        self,
        engine: ShortFormEngine,
        a: Admitted,
        spec: ShortFormSpec,
        plan: _Plan,
        set_id: UUID,
        began: float,
        probe: Callable[[], Awaitable[bool]] | None = None,
    ) -> None:
        self._probe = probe
        self._e, self._a, self._spec, self._plan = engine, a, spec, plan
        self._set_id, self._began = set_id, began
        self.queue: asyncio.Queue[Frame | None] = asyncio.Queue()
        self.handle = _ProducerHandle(engine._obs)
        self._first: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        self._suppress = a.profile.compliance  # API-7: phase 1 withheld, verdict frame only
        self._started = False
        self._committed = False  # the 200 went out on a heartbeat, before any frame

    async def first_byte(self) -> None:
        """Wait until the response status is decided (API-6); raises a failure before it.

        On the blocking path the first byte is the first heartbeat, so a failure inside
        ``heartbeat_seconds`` is still HTTP and a later one is the terminal ``error`` frame.
        """
        wait = self._e._cfg.heartbeat_seconds if self._suppress else None
        await asyncio.wait({self._first}, timeout=wait)
        if self._first.done():
            self._first.result()
        else:
            self._committed = True  # blocking path: the first heartbeat commits the status

    def _emit(self, frame: Frame) -> None:
        if not self._first.done():
            self._first.set_result(None)
            self._e._obs.first_frame(time.monotonic() - self._began, self._suppress)
        self.queue.put_nowait(frame)

    async def run(self) -> None:
        """The producer task."""
        try:
            with self._e._binder.bind(self._a.identity.tenant_id):
                await self._produce()
        except Exception as exc:  # noqa: BLE001 - every failure is closed on the run and reported
            await self._fail(exc)
        except asyncio.CancelledError:  # process shutdown: close the run, then stop
            await asyncio.shield(self._fail(RuntimeError("cancelled")))
            raise
        finally:
            if not self._first.done():
                self._first.set_result(None)
            self.queue.put_nowait(None)

    async def _produce(self) -> None:
        e, a, spec, plan = self._e, self._a, self._spec, self._plan
        await e._lifecycle.start(a)
        self._started = True
        parts: list[str] = []
        last = StreamChunk()
        usage = (0, 0)
        stream = e._gw.generate_stream(
            TASK_DRAFT,
            plan.composed.system,
            plan.composed.user,
            ctx=plan.ctx,
            deadline_seconds=spec.deadline,
        )
        async with e._obs.span("generation.draft"):
            async for chunk in stream:
                if chunk.delta:
                    before = "".join(parts)
                    parts.append(chunk.delta)
                    if not self._suppress:
                        new = _refs(before + chunk.delta) - _refs(before)
                        self._emit(DraftFrame(chunk.delta, tuple(sorted(new))))
                if chunk.model or chunk.serving_tier:
                    last = chunk
                if chunk.prompt_tokens or chunk.completion_tokens:
                    usage = (chunk.prompt_tokens, chunk.completion_tokens)
        text = "".join(parts)
        if not text.strip():
            raise GatewayError("the model returned no text")
        label = spec.profile_section if a.profile.section_type != "*" else None
        e._obs.tokens(TASK_DRAFT, last.model or "unknown", label or "other", sum(usage))
        gw = e._gw
        async with e._obs.span("generation.verify"):
            result = await e._verify.execute(
                VerificationRequest(
                    text=text,
                    evidence=plan.evidence,
                    refs=plan.composed.refs,
                    mandatory_clauses=a.profile.mandatory_clauses,
                    compliance=a.profile.compliance,
                    entailer=GatewayEntailer(gw, plan.ctx),
                    judge=GatewayJudge(gw, plan.ctx),
                    resampler=GatewayResampler(
                        gw, plan.ctx, plan.composed.system, plan.composed.user, spec.deadline
                    ),
                    section_label=label,
                )
            )
        if result.disposition.value != "surface":
            e._obs.failure_policy(a.profile.policy)
        await self._probe_client()
        delivery = (
            DeliveryState.DISCONNECTED if self.handle.disconnected else DeliveryState.DELIVERED
        )
        markers = tuple(merge_markers(plan.markers, result.markers))
        completion = CompletionInput(
            admitted=a,
            retrieval_set_id=self._set_id,
            content=text.encode(),
            prompt_hash=plan.composed.prompt_hash,
            model_profile_id=last.model or "unknown",
            serving_tier=last.serving_tier,
            verification=result,
            delivery=delivery,
            markers=plan.markers,
            disclaimer_ref=a.profile.disclaimer_ref if a.profile.compliance else None,
            parser_version=e._cfg.parser_version,
            chunker_version=e._cfg.chunker_version,
        )
        async with e._obs.span("generation.complete"):
            if spec.regeneration is not None:
                section = await e._complete.execute_regeneration(completion, spec.regeneration)
            else:
                completed = await e._complete.execute(completion)
        e._obs.delivery(delivery.value)
        e._obs.verified(spec.metric_kind, time.monotonic() - self._began)
        if spec.regeneration is not None:
            self._emit(
                replace(
                    build_verdict(
                        a,
                        result,
                        retrieval_set_id=self._set_id,
                        version_id=None,
                        version_no=None,
                        delivery=delivery,
                        markers=markers,
                        disclaimer_ref=a.profile.disclaimer_ref if a.profile.compliance else None,
                    ),
                    section_version_id=section.section_version.id,
                    prior_section_version_id=section.prior_section_version_id,
                )
            )
            return
        self._emit(
            build_verdict(
                a,
                result,
                retrieval_set_id=self._set_id,
                version_id=completed.version.id,
                version_no=completed.version.version_no,
                delivery=delivery,
                markers=markers,
                disclaimer_ref=completed.version.disclaimer_ref,
            )
        )

    async def _probe_client(self) -> None:
        if self.handle.disconnected or self._probe is None:
            return
        try:
            if await self._probe():
                self.handle.disconnected = True
        except Exception as exc:  # noqa: BLE001 - an unreadable connection is not a verdict
            log.warning("disconnect_probe_failed", error=type(exc).__name__)

    async def _fail(self, exc: BaseException) -> None:
        """Close the run, then report: HTTP before byte one, the ``error`` frame after (API-6)."""
        e, a = self._e, self._a
        reason, problem = failure_for(exc)
        try:
            if self._started:
                await asyncio.shield(e._lifecycle.fail(a, reason))
            else:
                await asyncio.shield(e._lifecycle.cancel(a, reason.value))
        except Exception as close_error:  # noqa: BLE001 - the sweep reaps it (§21.2)
            log.warning("run_close_failed", error=type(close_error).__name__)
        if self._committed or self._first.done():
            if not self._first.done():
                self._first.set_result(None)
            self.queue.put_nowait(frame_error(exc))
        else:
            self._first.set_exception(problem if problem is not None else exc)


class GenerateAnswer:
    """R-2 ``POST /api/v1/answers``: a grounded answer, two-phase streaming (§5.7.2)."""

    def __init__(self, engine: ShortFormEngine, settings: GenerationSettings) -> None:
        self._engine, self._cfg = engine, settings

    async def execute(
        self,
        identity: RequestIdentity,
        cmd: AnswerCommand,
        *,
        is_disconnected: Callable[[], Awaitable[bool]] | None = None,
    ) -> GenerationStream:
        """Admit and dispatch; the returned stream carries phase 1 and the verdict frame."""
        digest = request_hash(
            "answer",
            cmd.question,
            cmd.scope.tender_id,
            sorted(map(str, cmd.scope.collection_ids)),
            sorted(cmd.scope.collection_types),
            cmd.requested_mode.value,
        )
        admission = AdmissionRequest(
            kind=ArtefactKind.ANSWER,
            capability=ANSWER_CAPABILITY,
            section_type=ANSWER_CAPABILITY,
            user_text=cmd.question,
            scope=cmd.scope,
            required_level=GrantLevel.ANSWER,
            requested_mode=cmd.requested_mode,
            tender_id=cmd.scope.tender_id,
            idempotency_key=cmd.idempotency_key,
            request_hash=digest,
            correlation_id=cmd.correlation_id,
        )
        return await self._engine.start(
            identity,
            ShortFormSpec(
                task="answer",
                kind=ArtefactKind.ANSWER,
                capability=ANSWER_CAPABILITY,
                section_type=ANSWER_CAPABILITY,
                profile_section=None,
                query=cmd.question,
                user_prompt=cmd.question,
                scope=cmd.scope,
                required_level=GrantLevel.ANSWER,
                admission=admission,
                enforce_required=False,  # R-2 has no section type: no rule to apply (§5.7.2)
                deadline=self._cfg.answer_deadline_seconds,
                metric_kind="answer",
            ),
            is_disconnected=is_disconnected,
        )


class GenerateSectionDraft:
    """R-3 ``POST /api/v1/sections/drafts``: an interactive section draft (§5.7.3)."""

    def __init__(self, engine: ShortFormEngine, settings: GenerationSettings) -> None:
        self._engine, self._cfg = engine, settings

    async def execute(
        self,
        identity: RequestIdentity,
        cmd: DraftCommand,
        *,
        is_disconnected: Callable[[], Awaitable[bool]] | None = None,
    ) -> GenerationStream:
        """Admit and dispatch; ``422 REQUIRED_INPUT_MISSING`` comes before the first frame."""
        digest = request_hash(
            "draft",
            cmd.tender_id,
            cmd.tender_section_ref,
            cmd.section_type,
            cmd.user_prompt,
            sorted(map(str, cmd.scope.collection_ids)),
            sorted(cmd.scope.collection_types),
            cmd.requested_mode.value,
        )
        admission = AdmissionRequest(
            kind=ArtefactKind.SECTION_DRAFT,
            capability=cmd.section_type,
            section_type=cmd.section_type,
            user_text=cmd.user_prompt,
            scope=cmd.scope,
            required_level=GrantLevel.GENERATE,
            requested_mode=cmd.requested_mode,
            profile_section=cmd.section_type,
            tender_id=cmd.tender_id,
            tender_section_ref=cmd.tender_section_ref,
            idempotency_key=cmd.idempotency_key,
            request_hash=digest,
            correlation_id=cmd.correlation_id,
        )
        # The section type is the retrieval query's anchor: a draft with no prompt still needs one.
        query = f"{cmd.section_type} {cmd.user_prompt}".strip()
        return await self._engine.start(
            identity,
            ShortFormSpec(
                task="draft",
                kind=ArtefactKind.SECTION_DRAFT,
                capability=cmd.section_type,
                section_type=cmd.section_type,
                profile_section=cmd.section_type,
                query=query,
                user_prompt=cmd.user_prompt,
                scope=cmd.scope,
                required_level=GrantLevel.GENERATE,
                admission=admission,
                enforce_required=True,
                deadline=self._cfg.draft_deadline_seconds,
                metric_kind="draft",
            ),
            is_disconnected=is_disconnected,
        )
