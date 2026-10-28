"""R-4: structured extraction, synchronous under the 90 s SLO, never streamed (LLD §7.7, §13.10).

One ``extract`` call at temperature 0 against the *resolved* Domain Profile schema. Output is
validated before the artefact is written; a violation is non-retryable and the raw output is
kept for diagnosis (WP-8). Every field carries provenance or is a gap (GEN-8). The work after
admission runs in its own task and the handler awaits it shielded, so a client that drops the
connection still gets its artefact (GEN-2).
"""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass
from typing import Any
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
    merge_markers,
    request_hash,
)
from rag.application.generation.generate import _prompt_chunk, failure_for
from rag.application.generation.ports import ArtefactContentStore, TenantBinder
from rag.application.generation.requests import ExtractCommand
from rag.application.retrieval.dtos import PipelineRequest
from rag.application.retrieval.pipeline import RetrievalPipeline
from rag.application.retrieval.record import RecordRetrievalSet
from rag.application.shared.gateway import TASK_EXTRACT, GatewayContext, ModelGateway
from rag.application.shared.observability import GenerationObserver, NullGenerationObserver
from rag.application.shared.ports import TransactionRunner
from rag.application.verification.verify import ClaimResult, VerificationResult
from rag.domain.generation.eligibility import EligibilityVerdict
from rag.domain.generation.extraction import (
    GAP_NO_EVIDENCE,
    ExtractionResult,
    all_gaps,
    fields_to_json,
    parse_extraction,
)
from rag.domain.generation.prompt import compose_prompt, layer_texts, screen_layers
from rag.domain.generation.repositories import ArtefactReadRepository
from rag.domain.packs.generation import ExtractionSchema
from rag.domain.packs.profile import PackBundle
from rag.domain.shared.enums import (
    ArtefactKind,
    ClaimOutcome,
    CompletenessVerdict,
    DeliveryState,
    EvidenceTier,
    GrantLevel,
    RetrievalVerdict,
    RunStage,
)
from rag.domain.shared.errors import DomainError, ErrorCode
from rag.domain.shared.identity import RequestIdentity
from rag.domain.verification.completeness import Completeness
from rag.domain.verification.scoring import combined_verdict, derive_score_and_tier
from rag.domain.verification.tiers import value_in_text

log = get_logger(__name__)
EXTRACT_CAPABILITY = "extract"
INSUFFICIENT = "insufficient_evidence"


@dataclass(frozen=True, slots=True)
class ExtractionOutcome:
    """R-4's response body (§5.7.4). ``206`` where ``degradation_markers`` is non-empty."""

    artefact_id: UUID
    generation_id: UUID
    schema_version: str
    fields: dict[str, dict[str, Any]]
    eligibility: EligibilityVerdict
    degradation_markers: tuple[dict[str, str], ...] = ()
    evidence_tier: EvidenceTier = EvidenceTier.NOT_COMPUTED
    artefact_version_id: UUID | None = None
    retrieval_set_id: UUID | None = None
    refusal: dict[str, str] | None = None
    replay: bool = False

    @property
    def degraded(self) -> bool:
        """Derived, never set by hand (§5.7.4): any marker means reduced quality."""
        return len(self.degradation_markers) > 0


def verify_fields(result: ExtractionResult, texts: dict[str, str]) -> VerificationResult:
    """Tier-1 verification of an extraction: each value must appear in a chunk it cites.

    A value found only in another retrieved chunk is miscited (corrected), one found nowhere is
    unsupported and kept, flagged (VER-2). A gap counts as a gap. Score and tier use the same
    arithmetic as prose (§8.4); there is no mandatory-clause list, so completeness is not
    computed.
    """
    claims: list[ClaimResult] = []
    for ordinal, f in enumerate(result.fields, start=1):
        cited = tuple(p.chunk_id for p in f.provenance)
        if f.gap:
            claims.append(ClaimResult(ordinal, ClaimOutcome.GAP, (), (), 1))
        elif any(value_in_text(f.value, texts.get(c, "")) for c in cited):
            claims.append(ClaimResult(ordinal, ClaimOutcome.SUPPORTED, cited, (), 1))
        else:
            other = next(
                (
                    c
                    for c, t in sorted(texts.items())
                    if c not in cited and value_in_text(f.value, t)
                ),
                None,
            )
            outcome = ClaimOutcome.MISCITED if other else ClaimOutcome.UNSUPPORTED
            claims.append(ClaimResult(ordinal, outcome, cited, (other,) if other else (), 1))
    n = {o: sum(1 for c in claims if c.outcome is o) for o in ClaimOutcome}
    scored = derive_score_and_tier(
        supported=n[ClaimOutcome.SUPPORTED],
        unsupported=n[ClaimOutcome.UNSUPPORTED],
        miscited=n[ClaimOutcome.MISCITED],
        gaps=n[ClaimOutcome.GAP],
        compliance=False,
    )
    completeness = Completeness(CompletenessVerdict.NOT_COMPUTED, 0, 0)
    return VerificationResult(
        claims=tuple(claims),
        supported=n[ClaimOutcome.SUPPORTED],
        unsupported=n[ClaimOutcome.UNSUPPORTED],
        miscited=n[ClaimOutcome.MISCITED],
        gaps=n[ClaimOutcome.GAP],
        tiers_run=(1,),
        score=scored.score,
        tier=scored.tier,
        completeness=completeness,
        disposition=combined_verdict(scored.tier, completeness.verdict),
    )


def _query(schema: ExtractionSchema) -> str:
    named = ", ".join(f"{f.name} {f.description}".strip() for f in schema.fields)
    return f"{schema.description} {named}".strip() or schema.ref


def _skeleton(base: str, schema: ExtractionSchema) -> str:
    fields = "\n".join(
        f"- {f.name} ({f.type}{', conditional' if f.conditional else ''}): {f.description}"
        for f in schema.fields
    )
    return f"{base}\n\nSchema {schema.versioned_ref}. Fields:\n{fields}".strip()


class ExtractStructured:
    """R-4 ``POST /api/v1/extractions`` (§5.7.4)."""

    def __init__(
        self,
        *,
        admit: AdmitGeneration,
        lifecycle: RunLifecycle,
        complete: CompleteGeneration,
        pipeline: RetrievalPipeline,
        recorder: RecordRetrievalSet,
        gateway: ModelGateway,
        reads: ArtefactReadRepository,
        tx: TransactionRunner,
        store: ArtefactContentStore,
        binder: TenantBinder,
        settings: GenerationSettings,
        observer: GenerationObserver | None = None,
    ) -> None:
        self._admit, self._lifecycle, self._complete = admit, lifecycle, complete
        self._pipeline, self._recorder, self._gw = pipeline, recorder, gateway
        self._reads, self._tx, self._store, self._binder = reads, tx, store, binder
        self._cfg = settings
        self._obs = observer or NullGenerationObserver()
        self._tasks: set[asyncio.Task[ExtractionOutcome]] = set()

    async def drain(self, timeout: float) -> None:
        """Wait for extractions still running (graceful shutdown, GEN-2)."""
        if self._tasks:
            await asyncio.wait(set(self._tasks), timeout=timeout)

    async def execute(self, identity: RequestIdentity, cmd: ExtractCommand) -> ExtractionOutcome:
        """Admit, retrieve, extract, validate, verify, record. 200 body, or a §24 problem."""
        began = time.monotonic()
        schemas: dict[str, ExtractionSchema] = {}

        def check(bundle: PackBundle) -> None:
            found = bundle.domain_profile.extraction_schemas.get(cmd.schema_ref)
            if found is None:  # profile-declared, resolved per tenant (§5.7.4)
                raise DomainError(
                    ErrorCode.PACK_INVALID,
                    f"schema_ref is not declared by pack {bundle.domain_profile.version}.",
                )
            schemas["schema"] = found

        digest = request_hash(
            "extract",
            cmd.tender_id,
            cmd.schema_ref,
            sorted(map(str, cmd.document_ids)),
            sorted(map(str, cmd.scope.collection_ids)),
            sorted(cmd.scope.collection_types),
        )
        async with self._obs.span("extraction.admit"):
            adm = await self._admit.execute(
                identity,
                AdmissionRequest(
                    kind=ArtefactKind.EXTRACTION,
                    capability=EXTRACT_CAPABILITY,
                    section_type=cmd.schema_ref,
                    user_text="",
                    scope=cmd.scope,
                    required_level=GrantLevel.GENERATE,
                    tender_id=cmd.tender_id,
                    idempotency_key=cmd.idempotency_key,
                    request_hash=digest,
                    stage=RunStage.EXTRACT,
                    correlation_id=cmd.correlation_id,
                    check_pack=check,
                ),
            )
        if isinstance(adm, Replayed):
            return await self._replay(adm)
        task = asyncio.create_task(self._run(adm, cmd, schemas["schema"], began))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return await asyncio.shield(task)

    async def _run(
        self, a: Admitted, cmd: ExtractCommand, schema: ExtractionSchema, began: float
    ) -> ExtractionOutcome:
        with self._binder.bind(a.identity.tenant_id):
            try:
                async with self._obs.span("extraction.retrieve"):
                    pre = await self._pre_dispatch(a, cmd, schema)
            except asyncio.CancelledError:
                await asyncio.shield(self._lifecycle.cancel_quietly(a, "client_cancelled"))
                raise
            except DomainError as exc:
                await self._lifecycle.cancel_quietly(a, exc.code.value.lower())
                raise
            except Exception:
                await self._lifecycle.cancel_quietly(a, "internal")
                raise
            if isinstance(pre, ExtractionOutcome):
                return pre
            async with self._obs.span("extraction.dispatch"):
                return await self._dispatch(a, cmd, schema, pre, began)

    async def _pre_dispatch(
        self, a: Admitted, cmd: ExtractCommand, schema: ExtractionSchema
    ) -> ExtractionOutcome | _Dispatch:
        dp = a.bundle.domain_profile
        outcome = await self._pipeline.run(
            a.identity,
            PipelineRequest(
                query=_query(schema),
                scope=cmd.scope,
                required_level=GrantLevel.GENERATE,
                pinned_pack_version=None if a.bundle.is_platform_default else dp.version,
            ),
        )
        recorded = await self._recorder.record(
            a.identity, outcome, tender_id=cmd.tender_id, artefact_id=a.artefact.id
        )
        chunks = [
            c
            for c in outcome.chunks
            if not cmd.document_ids or c.document_id in set(cmd.document_ids)
        ]
        markers = tuple(merge_markers(m.as_dict() for m in (*a.pack_markers, *outcome.markers)))
        if outcome.verdict is not RetrievalVerdict.SUFFICIENT or not chunks:
            await self._lifecycle.cancel_quietly(a, INSUFFICIENT)
            return ExtractionOutcome(
                a.artefact.id,
                a.run.id,
                schema.versioned_ref,
                all_gaps(schema, GAP_NO_EVIDENCE),
                a.eligibility,
                markers,
                retrieval_set_id=recorded.retrieval_set_id,
                refusal={
                    "reason": INSUFFICIENT,
                    "detail": outcome.verdict_reason or "insufficient",
                },
            )
        prompt_chunks = [_prompt_chunk(c) for c in chunks]
        screen_layers(
            layer_texts(
                tenant_overrides=dp.tenant_overrides,
                user_context="",
                chunks=prompt_chunks,
                user_prompt="",
            ),
            dp.content_rules,
        )
        composed = compose_prompt(
            task="extract",
            skeleton=_skeleton(a.profile.skeleton or dp.answer_skeleton, schema),
            tenant_overrides=dp.tenant_overrides,
            user_context="",
            chunks=prompt_chunks,
            user_prompt="",
        )
        return _Dispatch(composed, prompt_chunks, recorded.retrieval_set_id, markers)

    async def _dispatch(
        self,
        a: Admitted,
        cmd: ExtractCommand,
        schema: ExtractionSchema,
        d: _Dispatch,
        began: float,
    ) -> ExtractionOutcome:
        ctx = GatewayContext(
            a.identity.plan, a.identity.residency_mode.value, tenant_id=a.identity.tenant_id
        )
        await self._lifecycle.start(a)
        try:
            done = await self._gw.complete(
                TASK_EXTRACT,
                d.composed.system,
                d.composed.user,
                ctx=ctx,
                deadline_seconds=self._cfg.extraction_deadline_seconds,
            )
            try:
                parsed = parse_extraction(schema, done.text, d.chunks, d.composed.refs)
            except DomainError:
                await self._retain_raw(a, done.text)  # WP-8: kept for diagnosis
                raise
            texts = {c.chunk_id: c.text for c in d.chunks}
            result = verify_fields(parsed, texts)
            fields = fields_to_json(parsed)
            body = json.dumps(
                {"schema_version": schema.versioned_ref, "fields": fields}, sort_keys=True
            ).encode()
            completed = await self._complete.execute(
                CompletionInput(
                    admitted=a,
                    retrieval_set_id=d.set_id,
                    content=body,
                    prompt_hash=d.composed.prompt_hash,
                    model_profile_id=done.model or "unknown",
                    serving_tier="primary",
                    verification=result,
                    delivery=DeliveryState.NOT_APPLICABLE,
                    markers=(),
                    output_schema_version=schema.versioned_ref,
                    trigger_document_version_id=cmd.trigger_document_version_id or "",
                    parser_version=self._cfg.parser_version,
                    chunker_version=self._cfg.chunker_version,
                )
            )
        except Exception as exc:
            reason, problem = failure_for(exc)
            try:
                await asyncio.shield(self._lifecycle.fail(a, reason))
            except Exception:  # noqa: BLE001 - the stalled-generation sweep reaps it (§21.2)
                log.warning("extraction_run_close_failed")
            if problem is not None and problem is not exc:
                raise problem from exc
            raise
        self._obs.delivery(DeliveryState.NOT_APPLICABLE.value)
        self._obs.verified("extraction", time.monotonic() - began)
        return ExtractionOutcome(
            a.artefact.id,
            a.run.id,
            schema.versioned_ref,
            fields,
            a.eligibility,
            tuple(merge_markers(d.markers, result.markers)),
            evidence_tier=result.tier,
            artefact_version_id=completed.version.id,
            retrieval_set_id=d.set_id,
        )

    async def _retain_raw(self, a: Admitted, raw: str) -> None:
        tenant = a.identity.tenant_id
        try:
            key = self._store.diagnostic_key(tenant, a.artefact.id, f"{a.run.id}-raw.json")
            await self._store.put(tenant, key, raw.encode())
        except Exception:  # noqa: BLE001 - diagnostics are best effort; the 422 stands
            log.warning("raw_extraction_output_not_retained")

    async def _replay(self, rep: Replayed) -> ExtractionOutcome:
        """The stored extraction, read back (§5.7.2)."""
        self._obs.idempotent_replay()

        async def read(tx: object) -> tuple[Any, Any]:
            view = await self._reads.get_artefact_view(tx, rep.artefact_id)
            version = view.current_version if view else None
            if version is None:
                raise DomainError(ErrorCode.ARTEFACT_NOT_FOUND, "The stored artefact is missing.")
            detail = await self._reads.get_version_detail(tx, rep.artefact_id, version.version_no)
            if detail is None or not detail.sections:
                raise DomainError(ErrorCode.ARTEFACT_NOT_FOUND, "The stored artefact is missing.")
            return version, detail.sections[0].section_version

        version, sv = await self._tx.run_in_tx(read, read_only=True)
        stored = json.loads(await self._store.get(version.tenant_id, sv.content_ref))
        return ExtractionOutcome(
            rep.artefact_id,
            rep.run.id,
            stored["schema_version"],
            stored["fields"],
            EligibilityVerdict(
                version.eligibility_state,
                version.requested_mode,
                version.effective_mode,
                version.clamped,
                version.domain_profile_version,
                version.clamp_reason,
            ),
            tuple(version.degradation_markers),
            evidence_tier=version.evidence_tier,
            artefact_version_id=version.id,
            retrieval_set_id=sv.retrieval_set_id,
            replay=True,
        )


@dataclass(frozen=True, slots=True)
class _Dispatch:
    composed: Any
    chunks: list[Any]
    set_id: UUID
    markers: tuple[dict[str, str], ...]


__all__ = ["ExtractStructured", "ExtractionOutcome", "verify_fields"]
