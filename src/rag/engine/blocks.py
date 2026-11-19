"""The building blocks the Worker Pool composes (workers LLD §4.2).

Thin facades over the code the API service's own routes use, so a worker and a request handler
resolve scope, compose prompts, call the Gateway and verify in exactly one way.

Each facade translates the application's refusals into the engine's typed errors once, so the
activity wrappers map a small, fixed set (``errors.py``). A quota refusal is ``QuotaRefused`` from
**every** model-calling block, not only the section path (workers LLD G-2), and every Gateway call
carries ``x-correlation-id = run:section:attempt`` (G-14).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from llm_models.workers import ExtractionResult
from rag.application.generation.extract import EXTRACT_CAPABILITY, ExtractStructured
from rag.application.generation.flow import AdmissionRequest, AdmitGeneration, Admitted
from rag.application.generation.refiner import InstructionRefiner, RefineRun
from rag.application.generation.requests import ExtractCommand
from rag.application.packs.ports import PackResolver
from rag.application.packs.resolve import resolve_or_default
from rag.application.retrieval.dtos import PipelineRequest, RetrievalOutcome
from rag.application.retrieval.pipeline import RetrievalPipeline
from rag.application.retrieval.record import RecordRetrievalSet
from rag.application.retrieval.scope import ScopeProvider, ScopeSpec
from rag.application.shared.gateway import (
    Completion,
    GatewayContext,
    GatewayDeadlineExceeded,
    GatewayError,
    GatewayQuotaRefused,
    GatewayResidencyRefused,
    ModelGateway,
)
from rag.application.verification.gateway import GatewayEntailer, GatewayJudge, GatewayResampler
from rag.application.verification.verify import (
    VerificationRequest,
    VerificationResult,
    VerifySection,
)
from rag.application.workflows.ports import PrincipalDirectory
from rag.domain.generation.facts import DocumentFact
from rag.domain.generation.prompt import ComposedPrompt, PromptChunk
from rag.domain.generation.prompt import compose_prompt as _compose
from rag.domain.packs.profile import PackBundle
from rag.domain.shared.enums import ArtefactKind, GrantLevel, RunStage, SectionState
from rag.domain.shared.errors import DomainError, ErrorCode
from rag.domain.shared.identity import RequestIdentity
from rag.engine.context import RunContext
from rag.engine.errors import (
    InferenceUnavailable,
    QuotaRefused,
    ResidencyRefused,
    RunGone,
    SchemaViolation,
    ScopeRevoked,
)

TASK_DRAFT = "draft"


@dataclass(frozen=True, slots=True)
class Blocks:
    """The collaborators of the facades. Built once by ``rag.engine.bootstrap``."""

    pipeline: RetrievalPipeline
    recorder: RecordRetrievalSet
    scope: ScopeProvider
    packs: PackResolver
    gateway: ModelGateway
    directory: PrincipalDirectory
    verifier: VerifySection
    refiner: InstructionRefiner
    extractor: ExtractStructured
    admit: AdmitGeneration


@dataclass(frozen=True, slots=True)
class ScopeSnapshot:
    """What ``reresolve`` found: counts and whether the scope narrowed. Never the scope token."""

    collections: int
    narrowed: bool


@dataclass(frozen=True, slots=True)
class Retrieved:
    """A retrieval and the set that recorded it. Chunk text stays in this process (WB-5)."""

    outcome: RetrievalOutcome
    retrieval_set_id: UUID


@dataclass(frozen=True, slots=True)
class ModelAnswer:
    """One model call's text and what the Gateway reported. In memory only."""

    text: str
    model: str
    serving_tier: str
    prompt_tokens: int
    completion_tokens: int
    finish_reason: str


def _blocks(ctx: RunContext) -> Blocks:
    blocks = ctx.services.blocks
    if blocks is None:
        raise RuntimeError("the engine was built without its building blocks")
    return blocks  # type: ignore[no-any-return]


def translate(exc: Exception) -> Exception:
    """The engine's error for an application refusal; any other exception comes back unchanged."""
    if isinstance(exc, GatewayQuotaRefused):
        return QuotaRefused()
    if isinstance(exc, GatewayResidencyRefused):
        return ResidencyRefused()
    if isinstance(exc, (GatewayError, GatewayDeadlineExceeded, TimeoutError)):
        return InferenceUnavailable()
    if isinstance(exc, DomainError):
        match exc.code:
            case ErrorCode.QUOTA_EXHAUSTED:
                return QuotaRefused()
            case ErrorCode.RESIDENCY_CAPACITY:
                return ResidencyRefused()
            case ErrorCode.INFERENCE_UNAVAILABLE:
                return InferenceUnavailable()
            case ErrorCode.COLLECTION_NOT_FOUND | ErrorCode.GRANT_LEVEL_INSUFFICIENT:
                return ScopeRevoked()
            case ErrorCode.EXTRACTION_SCHEMA_VIOLATION:
                return SchemaViolation()
    return exc


async def _identity(ctx: RunContext) -> RequestIdentity:
    """The principal's roles and departments, re-read from IAM (ASYNC-11): never from the input."""
    inp = ctx.input
    return await _blocks(ctx).directory.identity_for(
        ctx.tenant_id, ctx.principal_id, plan=inp.plan, residency_mode=inp.residency_mode
    )


def _spec(ctx: RunContext) -> ScopeSpec:
    seed = ctx.input.scope_seed
    return ScopeSpec(
        tender_id=None if seed.tender_id is None else UUID(seed.tender_id),
        collection_ids=tuple(UUID(c) for c in seed.collection_ids),
        collection_types=seed.collection_types,
    )


def gateway_context(ctx: RunContext, section_ref: str = "run", attempt: int = 1) -> GatewayContext:
    """Routing inputs for a Gateway call, with ``run:section:attempt`` as its correlation id."""
    inp = ctx.input
    return GatewayContext(
        inp.plan,
        inp.residency_mode,
        tenant_id=ctx.tenant_id,
        correlation_id=f"{inp.generation_run_id}:{section_ref}:{attempt}",
    )


async def load_pack(ctx: RunContext) -> PackBundle:
    """The pack version the run pinned at start, never the latest (``ASYNC-3``)."""
    bundle, _ = await resolve_or_default(
        _blocks(ctx).packs, ctx.tenant_id, pinned_version=ctx.input.domain_profile_version
    )
    return bundle


async def reresolve(ctx: RunContext, level: GrantLevel = GrantLevel.GENERATE) -> ScopeSnapshot:
    """Re-read the principal's memberships from IAM and re-resolve scope (``WP-4``, ``ASYNC-11``).

    Raises ``ScopeRevoked`` where nothing is left. A read that fails raises as it is: the step fails
    closed and never falls back to a default scope (``SCOPE-6``).
    """
    try:
        resolved = await _blocks(ctx).scope.resolve(await _identity(ctx), _spec(ctx), level)
    except DomainError as exc:
        raise translate(exc) from exc
    return ScopeSnapshot(len(resolved.collections), resolved.clamped)


async def retrieve(
    ctx: RunContext, query: str, *, section_type: str | None = None, limit: int | None = None
) -> Retrieved:
    """The retrieval pipeline over the run's pinned pack, and the set that records it."""
    b = _blocks(ctx)
    identity = await _identity(ctx)
    try:
        outcome = await b.pipeline.run(
            identity,
            PipelineRequest(
                query=query,
                scope=_spec(ctx),
                section_type=section_type,
                limit=limit,
                required_level=GrantLevel.GENERATE,
                pinned_pack_version=ctx.input.domain_profile_version,
            ),
        )
        recorded = await b.recorder.record(
            identity,
            outcome,
            tender_id=_uuid(ctx.input.tender_id),
            artefact_id=UUID(ctx.input.artefact_id),
        )
    except (DomainError, GatewayError) as exc:
        raise translate(exc) from exc
    return Retrieved(outcome, recorded.retrieval_set_id)


def _uuid(value: str | None) -> UUID | None:
    return None if value is None else UUID(value)


def compose_prompt(
    *,
    task: str,
    skeleton: str,
    tenant_overrides: str,
    user_context: str,
    chunks: Sequence[PromptChunk],
    user_prompt: str,
) -> ComposedPrompt:
    """The six-layer prompt for a task (``answer``, ``draft`` or ``extract``)."""
    return _compose(
        task=task,
        skeleton=skeleton,
        tenant_overrides=tenant_overrides,
        user_context=user_context,
        chunks=chunks,
        user_prompt=user_prompt,
    )


async def call_model(
    ctx: RunContext,
    task: str,
    prompt: ComposedPrompt,
    *,
    section_ref: str,
    attempt: int,
    deadline_seconds: float,
) -> ModelAnswer:
    """One model call through the Gateway with the run's routing context and correlation id.

    ``draft`` streams and is collected here (a worker has no browser to stream to); the JSON tasks
    (``extract``, ``refine-query``) are one completion. A refusal is a typed engine error.
    """
    gw, gctx = _blocks(ctx).gateway, gateway_context(ctx, section_ref, attempt)
    try:
        if task != TASK_DRAFT:
            done: Completion = await gw.complete(
                task, prompt.system, prompt.user, ctx=gctx, deadline_seconds=deadline_seconds
            )
            return ModelAnswer(done.text, done.model, "primary", 0, 0, "stop")
        parts: list[str] = []
        model = tier = finish = ""
        usage = (0, 0)
        async for chunk in gw.generate_stream(
            task, prompt.system, prompt.user, ctx=gctx, deadline_seconds=deadline_seconds
        ):
            parts.append(chunk.delta)
            model, tier = chunk.model or model, chunk.serving_tier or tier
            finish = chunk.finish_reason or finish
            if chunk.prompt_tokens or chunk.completion_tokens:
                usage = (chunk.prompt_tokens, chunk.completion_tokens)
        return ModelAnswer("".join(parts), model, tier or "primary", usage[0], usage[1], finish)
    except (GatewayError, TimeoutError) as exc:
        raise translate(exc) from exc


async def verify(
    ctx: RunContext,
    request: VerificationRequest,
    *,
    prompt: ComposedPrompt,
    deadline_seconds: float,
    section_ref: str = "run",
    attempt: int = 1,
) -> VerificationResult:
    """The verification ladder on a text, its model ports wired to the run's Gateway context."""
    b = _blocks(ctx)
    gw, gctx = b.gateway, gateway_context(ctx, section_ref, attempt)
    wired = VerificationRequest(
        text=request.text,
        evidence=request.evidence,
        refs=request.refs,
        mandatory_clauses=request.mandatory_clauses,
        compliance=request.compliance,
        entailer=GatewayEntailer(gw, gctx),
        judge=GatewayJudge(gw, gctx),
        resampler=GatewayResampler(gw, gctx, prompt.system, prompt.user, deadline_seconds),
        section_label=request.section_label,
    )
    try:
        return await b.verifier.execute(wired)
    except (DomainError, GatewayError) as exc:
        raise translate(exc) from exc


async def refine_instruction(
    ctx: RunContext,
    *,
    section_text: str,
    section_type: str,
    mandatory_clauses: Sequence[str],
    facts: Sequence[DocumentFact],
    bundle: PackBundle,
) -> RefineRun | None:
    """The Instruction Refiner for a regeneration run (flag-gated, ``None`` where it is off).

    The reviewer's words are read back from the run's stored input by run id, never from the
    workflow input (``instruction_ref``, workers LLD P0-4).
    """
    s = ctx.services

    async def read(tx: Any) -> str | None:
        return await s.engine.stored_instruction(tx, ctx.run_id)

    instruction = await s.tx.run_in_tx(read, read_only=True)
    if not instruction:
        return None
    try:
        return await _blocks(ctx).refiner.refine(
            instruction=instruction,
            section_text=section_text,
            section_type=section_type,
            mandatory_clauses=mandatory_clauses,
            facts=facts,
            bundle=bundle,
            ctx=gateway_context(ctx, section_type),
        )
    except (DomainError, GatewayError) as exc:
        raise translate(exc) from exc


async def run_extraction(ctx: RunContext) -> ExtractionResult:
    """W-6's body: search, extract, validate, verify, save and complete, the code R-4 runs.

    The shell and the run exist (the API admitted them) and ``begin_stage(extract)`` opened the
    run, so this rebuilds the admission from the stored rows under the run's *pinned* pack, then
    hands it to the extractor. A failure closes the run as R-4's does (``schema_violation``,
    ``inference_unavailable``, ...); the workflow's ``fail_run`` finds it terminal.
    """
    s, b, inp = ctx.services, _blocks(ctx), ctx.input
    schema_ref = inp.extras.get("schema_ref")
    if not schema_ref or inp.tender_id is None:
        raise ValueError("an extraction input names its schema_ref and its tender")
    identity = await _identity(ctx)
    schemas: dict[str, Any] = {}

    def check(bundle: PackBundle) -> None:
        found = bundle.domain_profile.extraction_schemas.get(schema_ref)
        if found is None:
            raise DomainError(ErrorCode.PACK_INVALID, "schema_ref is not declared by the pack.")
        schemas["schema"] = found

    request = AdmissionRequest(
        kind=ArtefactKind.EXTRACTION,
        capability=EXTRACT_CAPABILITY,
        section_type=schema_ref,
        user_text="",
        scope=_spec(ctx),
        required_level=GrantLevel.GENERATE,
        tender_id=UUID(inp.tender_id),
        stage=RunStage.EXTRACT,
        correlation_id=inp.correlation_id,
        check_pack=check,
        target_ref=inp.target_ref,
        pinned_pack_version=inp.domain_profile_version,
    )

    async def rows(tx: Any) -> tuple[Any, Any, Any]:
        run = await s.runs.get_run(tx, ctx.run_id)
        artefact = None if run is None else await s.runs.get_artefact(tx, run.artefact_id)
        found = [] if run is None else await s.engine.list_sections(tx, run.artefact_id)
        return run, artefact, found[0] if found else None

    run, artefact, section = await s.tx.run_in_tx(rows, read_only=True)
    if run is None or artefact is None or section is None:
        raise RunGone
    if section.state is not SectionState.GENERATING:
        section.start()  # the extraction's one section opens with the run (AS-5)
        await s.tx.run_in_tx(lambda tx: s.engine.save_section_state(tx, section))
    try:
        prep = await b.admit.prepare(identity, request)
        admitted = Admitted(
            identity=identity,
            request=request,
            scope=prep.scope,
            bundle=prep.bundle,
            pack_markers=tuple(prep.markers),
            eligibility=prep.eligibility,
            profile=prep.profile,
            artefact=artefact,
            run=run,
            section=section,
        )
        cmd = ExtractCommand(
            tender_id=UUID(inp.tender_id),
            schema_ref=schema_ref,
            scope=_spec(ctx),
            trigger_document_version_id=inp.extras.get("trigger_document_version_id"),
            correlation_id=inp.correlation_id,
        )
        outcome = await b.extractor.run_admitted(admitted, cmd, schemas["schema"])
    except (DomainError, GatewayError) as exc:
        raise translate(exc) from exc
    refusal = getattr(outcome, "refusal", None)
    return ExtractionResult(
        state="refused" if refusal else "complete",
        artefact_version_id=None
        if outcome.artefact_version_id is None
        else str(outcome.artefact_version_id),
        evidence_tier=outcome.evidence_tier.value,
        refusal_reason=None if not refusal else str(refusal.get("reason")),
    )
