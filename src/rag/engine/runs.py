"""The run-level writes of the engine (workers LLD §4.2, §2.2).

``run_state``, ``begin_stage``, ``suspend_for_quota``, ``complete_run`` and ``fail_run``.

Each write takes the run's row ``FOR UPDATE``, applies the domain machine (RUN-7), saves under the
same guard the API's gate routes use and publishes its event in the same transaction. A missing
row is ``RunGone``. A run already terminal is not an error: the function writes nothing and says
so, because the API service or a purge got there first and its outcome stands (WB-6, G-13).
"""

from __future__ import annotations

import copy
from datetime import datetime
from uuid import UUID

from llm_models.workers import MintedVersion, RunState, SectionRef
from rag.application.events.contracts import (
    GenerationCompleted,
    GenerationFailed,
    build_event,
    tender_subject,
)
from rag.application.shared.ports import TxHandle
from rag.application.workflows.common import publish_run_progress, recipients_of
from rag.domain.generation import audit
from rag.domain.generation.artefact import Artefact
from rag.domain.generation.run import GenerationRun
from rag.domain.shared.enums import (
    ArtefactKind,
    ArtefactOrigin,
    ArtefactStatus,
    AuditActorType,
    CompletenessVerdict,
    DeliveryState,
    EvidenceTier,
    OutlineState,
    RunFailureReason,
    RunStage,
    RunStatus,
)
from rag.engine.context import RunContext
from rag.engine.errors import RunGone


def _iso(at: datetime | None) -> str | None:
    return None if at is None else at.isoformat()


async def _locked(ctx: RunContext, tx: TxHandle) -> GenerationRun:
    run = await ctx.services.runs.get_run(tx, ctx.run_id, lock=True)
    if run is None:
        raise RunGone
    return run


async def _save(ctx: RunContext, tx: TxHandle, before: GenerationRun, after: GenerationRun) -> None:
    """One guarded update; the row is locked, so a miss is a bug and never a race."""
    version = await ctx.services.runs.save_gate(
        tx, after, expected_status=before.status, expected_record_version=before.record_version
    )
    if version is None:
        raise RuntimeError("run row changed under its own lock")
    after.record_version = version


async def run_state(ctx: RunContext) -> RunState:
    """The run's status, stage, outline state, gate deadline and each section (no write)."""

    async def read(tx: TxHandle) -> RunState:
        run = await ctx.services.runs.get_run(tx, ctx.run_id)
        if run is None:
            raise RunGone
        sections = await ctx.services.engine.list_sections(tx, run.artefact_id)
        suspended = await ctx.services.engine.suspended_at(tx, run.id)
        return RunState(
            status=run.status.value,
            stage=None if run.current_stage is None else run.current_stage.value,
            outline_state=run.outline_state.value,
            outline_version=run.outline_version,
            gate_expires_at=_iso(run.gate_expires_at),
            suspended_at=_iso(suspended),
            failure_reason=None if run.failure_reason is None else run.failure_reason.value,
            section_refs=tuple(
                SectionRef(
                    str(s.id),
                    s.tender_section_ref,
                    s.section_type,
                    s.is_foundation,
                    s.state.value,
                    s.ordinal,
                )
                for s in sections
            ),
        )

    return await ctx.services.tx.run_in_tx(read, read_only=True)


async def _open_artefact(ctx: RunContext, tx: TxHandle, run: GenerationRun) -> None:
    """P0-2: the artefact moves to ``generating`` when the run's first activity starts.

    A first run moves ``pending``; an A-5 regeneration finds a ``complete`` artefact (ART-5 allows
    ``complete -> generating``). Without this the orphan sweep, which selects ``pending``
    artefacts, would restart a live gated run on every pass.
    """
    artefact = await ctx.services.runs.get_artefact(tx, run.artefact_id, lock=True)
    if artefact is None:
        return
    regenerating = run.origin is not ArtefactOrigin.ORIGINAL
    if artefact.status is ArtefactStatus.PENDING or (
        regenerating and artefact.status is ArtefactStatus.COMPLETE
    ):
        artefact.move_to(ArtefactStatus.GENERATING)
        await ctx.services.runs.save_artefact_status(tx, artefact)


async def begin_stage(ctx: RunContext, stage: str) -> bool:
    """Write ``current_stage`` and the edge this stage begins; publish ``GenerationProgressed``.

    Returns ``False`` where the run is already terminal (the workflow stops). It writes **no edge**
    where the row already moved: after an approval, W-3 wrote ``generating`` (RUN-7), so
    ``begin_stage(stage1)`` writes the stage only. A repeat changes nothing and publishes nothing.
    """
    target = RunStage(stage)

    async def work(tx: TxHandle) -> bool:
        run = await _locked(ctx, tx)
        if run.is_terminal:
            return False
        after, changed = copy.copy(run), False
        if target is RunStage.OUTLINE:
            if after.outline_state in (OutlineState.NONE, OutlineState.REVISING):
                after.outline_started()
                changed = True
        elif after.status is RunStatus.PENDING:  # no gate: no outline wait preceded this stage
            after.move_to(RunStatus.GENERATING)
            changed = True
        if after.current_stage is not target:
            after.current_stage = target
            changed = True
        if not changed:
            return True
        await _save(ctx, tx, run, after)
        await _open_artefact(ctx, tx, after)
        await publish_run_progress(
            ctx.services.events, ctx.services.runs, ctx.services.clock, tx, after
        )
        return True

    return await ctx.services.tx.run_in_tx(work)


async def suspend_for_quota(ctx: RunContext) -> bool:
    """``generating -> suspended`` with ``quota_exhausted``, stamped ``suspended_at`` (G-3).

    Returns ``False`` where the run is terminal or no longer ``generating`` (a repeat).
    """

    async def work(tx: TxHandle) -> bool:
        run = await _locked(ctx, tx)
        if run.status is not RunStatus.GENERATING:
            return False
        after = copy.copy(run)
        after.move_to(RunStatus.SUSPENDED)
        after.failure_reason = RunFailureReason.QUOTA_EXHAUSTED
        await _save(ctx, tx, run, after)
        await ctx.services.engine.mark_suspended(tx, run.id, ctx.services.clock.now())
        await publish_run_progress(
            ctx.services.events, ctx.services.runs, ctx.services.clock, tx, after
        )
        return True

    return await ctx.services.tx.run_in_tx(work)


async def _close_artefact_after_failure(
    ctx: RunContext, tx: TxHandle, run: GenerationRun, artefact: Artefact | None
) -> None:
    """A failed original ends ``failed``; a failed regeneration returns to ``complete`` (AS-7)."""
    if artefact is None or artefact.status is not ArtefactStatus.GENERATING:
        return
    keeps_version = (
        run.origin is not ArtefactOrigin.ORIGINAL and artefact.current_version_id is not None
    )
    artefact.move_to(ArtefactStatus.COMPLETE if keeps_version else ArtefactStatus.FAILED)
    await ctx.services.runs.save_artefact_status(tx, artefact)


async def fail_run(ctx: RunContext, reason: str, stage: str | None = None) -> bool:
    """``failed`` with the reason, ``GenerationFailed`` and an audit entry, once.

    ``UPDATE ... WHERE status`` is non-terminal: a repeat finds a terminal row and changes and
    publishes nothing, so a run has exactly one terminal event (WE-5, G-13). Completed sections
    stay durable (AOB-5). Returns whether this call ended the run.
    """
    why = RunFailureReason(reason)
    at_stage = None if stage is None else RunStage(stage)

    async def work(tx: TxHandle) -> bool:
        run = await _locked(ctx, tx)
        if run.is_terminal:
            return False
        after = copy.copy(run)
        after.fail(why, at_stage or run.current_stage)
        await _save(ctx, tx, run, after)
        artefact = await ctx.services.runs.get_artefact(tx, run.artefact_id, lock=True)
        await _close_artefact_after_failure(ctx, tx, after, artefact)
        await ctx.services.runs.close_in_flight_sections(tx, run.artefact_id, why)
        done, total = await ctx.services.runs.count_sections(tx, run.artefact_id)
        s = ctx.services
        await s.audit(
            tx,
            audit.AuditEntryDraft(
                tenant_id=after.tenant_id,
                occurred_at=s.clock.now(),
                entry_type=audit.GENERATION_FAILED[0],
                action=audit.GENERATION_FAILED[1],
                actor_type=AuditActorType.SERVICE_ACCOUNT,
                actor_id=after.principal_id,
                artefact_id=after.artefact_id,
                source_event_type="GenerationFailed",
                source_event_id=f"GenerationFailed:{after.id}",
                target_type="artefact",
                target_id=str(after.artefact_id),
                metadata={
                    "generation_run_id": str(after.id),
                    "failure_reason": why.value,
                    "failed_at_stage": None
                    if after.failed_at_stage is None
                    else after.failed_at_stage.value,
                    "workflow_id": after.workflow_id,
                },
            ),
        )
        await s.events.publish(
            tx,
            build_event(
                after.tenant_id,
                tender_subject(after.tender_id),
                GenerationFailed(
                    artefact_id=str(after.artefact_id),
                    principal_id=str(after.principal_id),
                    reason=why,
                    sections_complete=done,
                    sections_total=total,
                    sections_durable=True,
                    generation_id=str(after.id),
                    tender_id=None if after.tender_id is None else str(after.tender_id),
                    failed_at_stage=after.failed_at_stage,
                    recipients=recipients_of(after),
                ),
                correlation_id=str((after.run_input or {}).get("correlation_id") or ""),
            ),
        )
        return True

    return await ctx.services.tx.run_in_tx(work)


async def complete_run(ctx: RunContext, minted: MintedVersion) -> bool:
    """``generating -> complete`` after ``mint_document_version``; ``GenerationCompleted`` once.

    The event carries ``sections_failed`` and ``sections_gap`` so a consumer never closes a ticket
    on a partial document (workers LLD, review compat row on event payloads). Returns whether this
    call completed the run.
    """

    async def work(tx: TxHandle) -> bool:
        run = await _locked(ctx, tx)
        if run.status is not RunStatus.GENERATING:
            return False
        after = copy.copy(run)
        after.move_to(RunStatus.COMPLETE)
        await _save(ctx, tx, run, after)
        artefact = await ctx.services.runs.get_artefact(tx, run.artefact_id, lock=True)
        if artefact is not None and artefact.status is ArtefactStatus.GENERATING:
            artefact.move_to(ArtefactStatus.COMPLETE)
            await ctx.services.runs.save_artefact_status(tx, artefact)
            await ctx.services.engine.set_artefact_current_version(
                tx, artefact.id, UUID(minted.version_id)
            )
        failed, gap, total = await ctx.services.engine.section_outcomes(tx, run.artefact_id)
        s = ctx.services
        await s.events.publish(
            tx,
            build_event(
                after.tenant_id,
                tender_subject(after.tender_id),
                GenerationCompleted(
                    artefact_id=str(after.artefact_id),
                    artefact_version_id=minted.version_id,
                    version_no=minted.version_no,
                    principal_id=str(after.principal_id),
                    kind=ArtefactKind.DOCUMENT,
                    minted_reason="regeneration"
                    if after.origin is not ArtefactOrigin.ORIGINAL
                    else "initial",
                    origin=after.origin,
                    evidence_tier=EvidenceTier(minted.evidence_tier),
                    completeness=CompletenessVerdict(minted.completeness),
                    delivery=DeliveryState.DELIVERED,
                    section_count=total,
                    generation_id=str(after.id),
                    tender_id=None if after.tender_id is None else str(after.tender_id),
                    coherence_verdict=after.coherence,
                    recipients=recipients_of(after),
                    sections_failed=failed,
                    sections_gap=gap,
                ),
                correlation_id=str((after.run_input or {}).get("correlation_id") or ""),
            ),
        )
        return True

    return await ctx.services.tx.run_in_tx(work)
