"""Facts, coherence, checkpoints and the document version (workers LLD §4.2).

``record_facts`` writes stage 1's document-wide facts, ``record_coherence`` the cross-section
verdict and its conflicts, ``mint_document_version`` rolls the sections up into one version,
``record_checkpoint`` an inspection row that never holds text.
"""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from dataclasses import dataclass
from uuid import UUID, uuid4

from llm_models.workers import CoherenceResult, MintedVersion
from rag.application.events.contracts import ArtefactRegenerated, build_event, tender_subject
from rag.application.shared.ports import TxHandle
from rag.application.workflows.common import recipients_of
from rag.domain.generation import audit
from rag.domain.generation.artefact import ArtefactVersion
from rag.domain.generation.evidence import roll_up_evidence_tier
from rag.domain.generation.facts import DocumentFact, FactConflict
from rag.domain.generation.run import GenerationRun
from rag.domain.shared.enums import (
    ArtefactOrigin,
    AuditActorType,
    CoherenceVerdict,
    CompletenessVerdict,
    DeliveryState,
    EvidenceTier,
    RunStatus,
)
from rag.engine.context import RunContext
from rag.engine.errors import NothingToMint, RunGone, RunTerminal


@dataclass(frozen=True, slots=True)
class FactDraft:
    """One document-wide fact a foundation section establishes (``DF-1``)."""

    fact_key: str
    value: str
    section_id: UUID


@dataclass(frozen=True, slots=True)
class ConflictDraft:
    """Two sections that state a fact differently. Values are short facts, never prose."""

    section_id: UUID
    fact_key: str
    established_value: str
    other_value: str
    established_by_section_id: UUID


async def _locked(ctx: RunContext, tx: TxHandle) -> GenerationRun:
    run = await ctx.services.runs.get_run(tx, ctx.run_id, lock=True)
    if run is None:
        raise RunGone
    return run


async def record_facts(ctx: RunContext, facts: Sequence[FactDraft]) -> int:
    """Write the document-wide facts of stage 1 (``DF-1``).

    Where a key already has a current fact (a regeneration) it is **superseded first**, because
    ``uq_fact_current`` allows a new current fact only after the old one is superseded (``DF-2``).
    A fact identical to the current one is left alone, so a re-run inserts nothing twice. Sets no
    review flags: a regeneration regenerates every section.
    """
    s = ctx.services

    async def work(tx: TxHandle) -> int:
        run = await _locked(ctx, tx)
        if run.is_terminal:
            raise RunTerminal
        current = {f.fact_key: f for f in await s.facts.current_facts(tx, run.artefact_id)}
        written = 0
        for f in facts:
            have = current.get(f.fact_key)
            if have is not None and have.value == f.value:
                continue
            if have is not None:
                await s.facts.supersede_fact(tx, run.artefact_id, f.fact_key, s.clock.now())
            await s.facts.insert_fact(
                tx,
                DocumentFact(
                    id=uuid4(),
                    artefact_id=run.artefact_id,
                    tenant_id=run.tenant_id,
                    fact_key=f.fact_key,
                    value=f.value,
                    established_by_section_id=f.section_id,
                ),
            )
            written += 1
        return written

    return await s.tx.run_in_tx(work)


async def record_coherence(
    ctx: RunContext, verdict: str, conflicts: Sequence[ConflictDraft]
) -> CoherenceResult:
    """Write the coherence verdict and surface each conflict for review.

    The verdict goes on ``generation_runs.coherence``, each conflict into ``fact_conflicts``, and
    the conflicting section is flagged. A conflict already open on the same section, key and
    values is not written again (``WP-16``: a second conflict is surfaced to the reviewer, never
    retried).
    """
    s = ctx.services
    result = CoherenceVerdict(verdict)

    async def work(tx: TxHandle) -> CoherenceResult:
        run = await _locked(ctx, tx)
        if run.is_terminal:
            raise RunTerminal
        await s.engine.set_coherence(tx, run.id, result, s.clock.now())
        surfaced = 0
        for c in conflicts:
            open_one = await s.facts.find_open(
                tx, c.section_id, c.fact_key, c.other_value, c.established_value
            )
            if open_one is None:
                await s.facts.insert_conflict(
                    tx,
                    FactConflict(
                        id=uuid4(),
                        tenant_id=run.tenant_id,
                        artefact_id=run.artefact_id,
                        section_id=c.section_id,
                        fact_key=c.fact_key,
                        established_value=c.established_value,
                        instruction_value=c.other_value,
                        established_by_section_id=c.established_by_section_id,
                    ),
                )
                surfaced += 1
            await s.engine.flag_section(tx, c.section_id, f"fact_conflict:{c.fact_key}")
        return CoherenceResult(result.value, surfaced)

    return await s.tx.run_in_tx(work)


async def record_checkpoint(
    ctx: RunContext, section_id: UUID, activity_run_id: str, nodes: Sequence[tuple[str, int, int]]
) -> None:
    """One inspection row for a section graph run: ``(node, milliseconds, count)`` per node.

    Names, timings and counts only. A stock LangGraph checkpointer stores the full state, text
    included, which would put content outside the content stores (``WA-11``); the worker therefore
    uses none and writes this instead (workers LLD G-12).
    """
    state = {"nodes": [{"node": n, "ms": ms, "count": c} for n, ms, c in nodes]}

    async def work(tx: TxHandle) -> None:
        await ctx.services.engine.insert_checkpoint(
            tx, ctx.tenant_id, section_id, activity_run_id, state
        )

    await ctx.services.tx.run_in_tx(work)


async def mint_document_version(ctx: RunContext) -> MintedVersion:
    """Roll the sections' current versions up into one document version (``AV-3``, ``REGEN-8``).

    The tier is the lowest section tier, and a section with no version counts as the lowest
    (``MD-30``, ``AOB-2``); completeness is ``complete`` only when every section is. Publishes
    ``ArtefactRegenerated`` for a regeneration and writes the audit entry with the content digest
    (``AC-8``). A repeat returns the version already minted for this run.
    """
    s = ctx.services

    async def work(tx: TxHandle) -> MintedVersion:
        run = await _locked(ctx, tx)
        if run.status is not RunStatus.GENERATING:
            raise RunTerminal
        current = await s.engine.current_section_versions(tx, run.artefact_id)
        if not current:
            raise NothingToMint
        failed, gap, total = await s.engine.section_outcomes(tx, run.artefact_id)
        # A section with no version counts as the lowest tier, so the roll-up never reads better
        # than the document is (MD-30, workers LLD WOQ-4: the TL confirms this rule).
        tiers = [c.evidence_tier for c in current] + [EvidenceTier.INSUFFICIENT] * (
            total - len(current)
        )
        tier = roll_up_evidence_tier(tiers)
        completeness = (
            CompletenessVerdict.COMPLETE
            if failed == 0 and gap == 0 and len(current) == total
            else CompletenessVerdict.PARTIAL
        )
        have = await s.engine.document_version_of_run(tx, run.id)
        if have is not None:  # a repeat: the version this run already minted
            return MintedVersion(str(have[0]), have[1], tier.value, completeness.value)
        version_no, _ = await s.writes.next_version_numbers(
            tx, run.artefact_id, current[0].section_id
        )
        regenerating = run.origin is not ArtefactOrigin.ORIGINAL
        # ponytail: a document has no single prompt; the hash is over its sections' hashes
        prompt_hash = hashlib.sha256("\n".join(c.prompt_hash for c in current).encode()).hexdigest()
        av = ArtefactVersion(
            id=uuid4(),
            artefact_id=run.artefact_id,
            tenant_id=run.tenant_id,
            version_no=version_no,
            minted_reason="regeneration" if regenerating else "initial",
            evidence_tier=tier,
            completeness=completeness,
            eligibility_state=run.eligibility_state,
            requested_mode=run.requested_mode,
            effective_mode=run.effective_mode,
            domain_profile_version=run.domain_profile_version,
            language_pack_version=run.language_pack_version,
            model_profile_id=current[0].model_profile_id,
            prompt_hash=prompt_hash,
            parser_version="unrecorded",
            chunker_version="unrecorded",
            generation_run_id=run.id,
            delivery=DeliveryState.DELIVERED,
            origin=run.origin,
            clamped=run.clamped,
            clamp_reason=run.clamp_reason,
            instruction=await s.engine.stored_instruction(tx, run.id) if regenerating else None,
        )
        await s.writes.insert_document_version(tx, av, [c.section_version_id for c in current])
        await s.audit(
            tx,
            audit.AuditEntryDraft(
                tenant_id=run.tenant_id,
                occurred_at=s.clock.now(),
                entry_type=audit.GENERATION_COMPLETED[0],
                action=audit.GENERATION_COMPLETED[1],
                actor_type=AuditActorType.SERVICE_ACCOUNT,
                actor_id=run.principal_id,
                artefact_id=run.artefact_id,
                source_event_type="GenerationCompleted",
                source_event_id=str(av.id),
                target_type="artefact_version",
                target_id=str(av.id),
                metadata={
                    "version_id": str(av.id),
                    "generation_run_id": str(run.id),
                    "prompt_hash": prompt_hash,
                    "model_profile_id": av.model_profile_id,
                    "domain_profile_version": av.domain_profile_version,
                    "language_pack_version": av.language_pack_version,
                    "evidence_tier": tier.value,
                    "completeness": completeness.value,
                    "content_digest": audit.content_digest(
                        [(c.section_version_id, c.content_sha256) for c in current]
                    ),
                    "section_count": total,
                },
            ),
        )
        if regenerating and run.base_version_id is not None:
            await s.events.publish(
                tx,
                build_event(
                    run.tenant_id,
                    tender_subject(run.tender_id),
                    ArtefactRegenerated(
                        artefact_id=str(run.artefact_id),
                        artefact_version_id=str(av.id),
                        version_no=version_no,
                        base_version_id=str(run.base_version_id),
                        principal_id=str(run.principal_id),
                        scope="document",
                        origin=run.origin,
                        evidence_tier=tier,
                        completeness=completeness,
                        tender_id=None if run.tender_id is None else str(run.tender_id),
                        recipients=recipients_of(run),
                    ),
                    correlation_id=str((run.run_input or {}).get("correlation_id") or ""),
                ),
            )
        return MintedVersion(str(av.id), version_no, tier.value, completeness.value)

    return await s.tx.run_in_tx(work)
