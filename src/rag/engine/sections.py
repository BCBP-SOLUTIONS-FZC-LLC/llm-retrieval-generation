"""The outline and section writes of the engine (workers LLD §4.2).

``save_outline`` writes the section rows (and, behind a gate, the wait), ``begin_section`` moves a
section to ``generating``, ``save_section`` and ``save_section_gap`` end it, ``fail_section``
closes one whose attempts ran out. Every write re-checks, under the run's lock, that the run is
not terminal and the section is still in the state the writer expects, so a cancelled run takes no
late write (WB-6) and a repeat changes nothing (WB-8).
"""

from __future__ import annotations

import copy
import hashlib
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import timedelta
from uuid import UUID, uuid4

from llm_models.workers import OutlineSaved, SectionOutcome
from rag.application.events.contracts import (
    GenerationGapReported,
    OutlineReady,
    SectionCompleted,
    build_event,
    tender_subject,
)
from rag.application.generation.flow import merge_markers, verification_rows
from rag.application.shared.ports import TxHandle
from rag.application.verification.verify import VerificationResult
from rag.application.workflows.common import publish_run_progress, recipients_of
from rag.domain.generation.repositories import CompletionRows
from rag.domain.generation.run import GenerationRun
from rag.domain.generation.section import ArtefactSection, SectionVersion
from rag.domain.shared.enums import ArtefactOrigin, OutlineState, RunFailureReason, SectionState
from rag.engine.context import RunContext
from rag.engine.errors import RunGone, RunTerminal
from rag.infrastructure.persistence.generation.engine import OutlineEntry


@dataclass(frozen=True, slots=True)
class SectionDraft:
    """What a finished section hands to ``save_section``.

    Content stays in memory, never in a result or an input (WB-5): it is written to S3 by
    ``save_section`` and nowhere else.
    """

    content: bytes
    retrieval_set_id: UUID
    prompt_hash: str
    model_profile_id: str
    serving_tier: str
    verification: VerificationResult
    markers: tuple[dict[str, str], ...] = ()
    scope_narrowed: bool = False
    auto_retry_count: int = 0


@dataclass(frozen=True, slots=True)
class GapDraft:
    """A section that cannot be written for lack of required input (§6.9). Entity names only."""

    reason: str
    missing_roles: tuple[str, ...] = ()
    unresolved_entities: tuple[str, ...] = ()
    empty_collections: tuple[str, ...] = ()
    blocking: bool = True


async def _locked(ctx: RunContext, tx: TxHandle) -> GenerationRun:
    run = await ctx.services.runs.get_run(tx, ctx.run_id, lock=True)
    if run is None:
        raise RunGone
    return run


def _ref(section: ArtefactSection) -> str:
    return section.tender_section_ref or str(section.id)


async def save_outline(
    ctx: RunContext, entries: Sequence[OutlineEntry], *, gated: bool
) -> OutlineSaved:
    """Write the outline as ``artefact_sections`` rows.

    **With a gate** it also moves the run to ``awaiting_outline_approval``, sets the outline timing
    and publishes ``OutlineReady``. **Without** (the pack declares none, a regeneration) it writes
    the section rows only: no run edge, no outline state, no event. A repeat returns what is
    already written and publishes nothing.
    """
    s = ctx.services

    async def work(tx: TxHandle) -> OutlineSaved:
        run = await _locked(ctx, tx)
        if run.is_terminal:
            raise RunTerminal
        existing = await s.engine.list_sections(tx, run.artefact_id)
        ids = tuple(str(x.id) for x in existing)
        gate_at = None if run.gate_expires_at is None else run.gate_expires_at.isoformat()
        if gated and run.outline_state is OutlineState.READY:
            return OutlineSaved(ids, run.outline_version, gate_at)  # a repeat of a finished write
        if not gated and existing:
            return OutlineSaved(ids, None, None)
        written = await s.engine.replace_outline(tx, run.artefact_id, run.tenant_id, entries)
        out_ids = tuple(str(x.id) for x in written)
        if not gated:
            return OutlineSaved(out_ids, None, None)
        now = s.clock.now()
        expires = now + timedelta(days=s.review_gate_days)
        after = copy.copy(run)
        after.outline_written(now)
        version = await s.runs.save_gate(
            tx, after, expected_status=run.status, expected_record_version=run.record_version
        )
        if version is None:
            raise RuntimeError("run row changed under its own lock")
        after.record_version = version
        await s.engine.set_outline_timing(tx, run.id, now, expires)
        await s.events.publish(
            tx,
            build_event(
                after.tenant_id,
                tender_subject(after.tender_id),
                OutlineReady(
                    artefact_id=str(after.artefact_id),
                    principal_id=str(after.principal_id),
                    outline_version=after.outline_version or 1,
                    section_count=len(written),
                    gate_expires_at=expires.isoformat().replace("+00:00", "Z"),
                    tender_id=None if after.tender_id is None else str(after.tender_id),
                    recipients=recipients_of(after),
                ),
                correlation_id=str((after.run_input or {}).get("correlation_id") or ""),
            ),
        )
        await publish_run_progress(s.events, s.runs, s.clock, tx, after)
        return OutlineSaved(out_ids, after.outline_version, expires.isoformat())

    return await s.tx.run_in_tx(work)


async def begin_section(ctx: RunContext, section_id: UUID) -> bool:
    """``pending|gap|failed|complete -> generating``. ``False`` means skip the section.

    Skip where the run is terminal, or the section's current version was written by this very run
    (a restarted workflow does not redo finished work, ``ASYNC-14``). A section already
    ``generating`` is proceed: the previous activity died and this attempt takes over (retry).
    """
    s = ctx.services

    async def work(tx: TxHandle) -> bool:
        run = await _locked(ctx, tx)
        if run.is_terminal:
            return False
        section = await s.engine.get_section(tx, section_id, lock=True)
        if section is None or section.artefact_id != run.artefact_id:
            raise RunGone
        created = run.created_at
        if created is not None and await s.engine.section_done_in_run(tx, section_id, created):
            return False
        if section.state is SectionState.GENERATING:
            return True
        section.start()
        await s.engine.save_section_state(tx, section)
        return True

    return await s.tx.run_in_tx(work)


def _version(
    ctx: RunContext, section: ArtefactSection, draft: SectionDraft, section_no: int, key: str
) -> SectionVersion:
    v = draft.verification
    markers = merge_markers(draft.markers, v.markers)
    return SectionVersion(
        id=uuid4(),
        section_id=section.id,
        tenant_id=ctx.tenant_id,
        version_no=section_no,
        content_ref=key,
        content_sha256=hashlib.sha256(draft.content).digest(),  # ASV-4: over the bytes written
        retrieval_set_id=draft.retrieval_set_id,
        evidence_tier=v.tier,
        completeness=v.completeness.verdict,
        model_profile_id=draft.model_profile_id,
        prompt_hash=draft.prompt_hash,
        serving_tier=draft.serving_tier or "primary",
        origin=ArtefactOrigin(ctx.input.origin),
        scope_narrowed=draft.scope_narrowed,
        auto_retry_count=draft.auto_retry_count,
        degradation_markers=markers,
    )


async def save_section(ctx: RunContext, section_id: UUID, draft: SectionDraft) -> SectionOutcome:
    """Write the finished section and publish ``SectionCompleted``.

    Content goes to S3, then one transaction writes the version, its verification rows and the
    section edge (``ASV-4``, ``AS-5``).

    Guarded on the run being non-terminal and the section still ``generating``: otherwise nothing
    is written and the outcome is ``skipped_terminal`` (WB-6). The cost is the tokens already
    spent. Reuses the completion write the synchronous paths use (``insert_completion`` with no
    document version, ``REGEN-8``), so there is one way to write a section version.
    """
    s = ctx.services

    async def work(tx: TxHandle) -> SectionOutcome:
        run = await _locked(ctx, tx)
        section = await s.engine.get_section(tx, section_id, lock=True)
        if section is None or section.artefact_id != run.artefact_id:
            raise RunGone
        if run.is_terminal or section.state is not SectionState.GENERATING:
            return SectionOutcome(str(section_id), section.state.value, skipped_terminal=True)
        _, section_no = await s.writes.next_version_numbers(tx, run.artefact_id, section.id)
        key = s.store.section_key(ctx.tenant_id, run.artefact_id, section.id, section_no)
        sv = _version(ctx, section, draft, section_no, key)
        await s.store.put(ctx.tenant_id, key, draft.content)
        report, claims, check = verification_rows(ctx.tenant_id, sv.id, draft.verification)
        await s.writes.insert_completion(tx, CompletionRows(None, sv, report, claims, check))
        section.complete(sv.id)
        await s.engine.save_section_state(tx, section)
        done, total = await s.runs.count_sections(tx, run.artefact_id)
        await s.events.publish(
            tx,
            build_event(
                run.tenant_id,
                tender_subject(run.tender_id),
                SectionCompleted(
                    artefact_id=str(run.artefact_id),
                    principal_id=str(run.principal_id),
                    section_ref=_ref(section),
                    state=SectionState.COMPLETE,
                    origin=ArtefactOrigin(ctx.input.origin),
                    sections_complete=done,
                    sections_total=total,
                    generation_id=str(run.id),
                    tender_id=None if run.tender_id is None else str(run.tender_id),
                    section_version_id=str(sv.id),
                    evidence_tier=sv.evidence_tier,
                    completeness=sv.completeness,
                    retrieval_set_id=str(sv.retrieval_set_id),
                    recipients=recipients_of(run),
                ),
                correlation_id=str((run.run_input or {}).get("correlation_id") or ""),
            ),
        )
        return SectionOutcome(
            str(section_id),
            SectionState.COMPLETE.value,
            version_id=str(sv.id),
            evidence_tier=sv.evidence_tier.value,
        )

    return await s.tx.run_in_tx(work)


async def save_section_gap(ctx: RunContext, section_id: UUID, gap: GapDraft) -> SectionOutcome:
    """End the section as a gap and publish ``GenerationGapReported`` (§6.9).

    A section that has a version stays ``complete`` instead (``AS-7``): the previous version
    remains current.
    """
    s = ctx.services

    async def work(tx: TxHandle) -> SectionOutcome:
        run = await _locked(ctx, tx)
        section = await s.engine.get_section(tx, section_id, lock=True)
        if section is None or section.artefact_id != run.artefact_id:
            raise RunGone
        if run.is_terminal or section.state is not SectionState.GENERATING:
            return SectionOutcome(str(section_id), section.state.value, skipped_terminal=True)
        section.end_without_version(gap_reason=gap.reason, failure=RunFailureReason.INTERNAL)
        await s.engine.save_section_state(tx, section)
        if section.gap_reason is not None:  # it ended a gap; a section with a version did not
            await s.events.publish(
                tx,
                build_event(
                    run.tenant_id,
                    tender_subject(run.tender_id),
                    GenerationGapReported(
                        artefact_id=str(run.artefact_id),
                        principal_id=str(run.principal_id),
                        section_ref=_ref(section),
                        missing_roles=list(gap.missing_roles),
                        unresolved_entities=list(gap.unresolved_entities),
                        empty_collections=list(gap.empty_collections),
                        blocking=gap.blocking,
                        tender_id=None if run.tender_id is None else str(run.tender_id),
                        recipients=recipients_of(run),
                    ),
                    correlation_id=str((run.run_input or {}).get("correlation_id") or ""),
                ),
            )
        return SectionOutcome(str(section_id), section.state.value, gap_reason=section.gap_reason)

    return await s.tx.run_in_tx(work)


async def fail_section(ctx: RunContext, section_id: UUID, reason: str) -> SectionOutcome:
    """Close one section ``failed`` with the run's reason vocabulary when its attempts ran out.

    A section that has a version returns to ``complete`` (``AS-7``). It publishes no event: a
    section failure is artefact state and moves no Tender state machine (``AOB-5``).
    """
    s = ctx.services
    why = RunFailureReason(reason)

    async def work(tx: TxHandle) -> SectionOutcome:
        run = await _locked(ctx, tx)
        section = await s.engine.get_section(tx, section_id, lock=True)
        if section is None or section.artefact_id != run.artefact_id:
            raise RunGone
        if run.is_terminal or section.state is not SectionState.GENERATING:
            return SectionOutcome(str(section_id), section.state.value, skipped_terminal=True)
        section.end_without_version(gap_reason=None, failure=why)
        await s.engine.save_section_state(tx, section)
        return SectionOutcome(
            str(section_id),
            section.state.value,
            failure_reason=None if section.failure_reason is None else section.failure_reason.value,
        )

    return await s.tx.run_in_tx(work)
