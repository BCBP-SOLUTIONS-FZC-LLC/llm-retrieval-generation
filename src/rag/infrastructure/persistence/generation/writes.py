"""Admission and completion SQL of the generation write path (LLD §7.5, §14.1, RUN-1, GEN-10).

Runs on the connection ``run_in_tx`` hands over, inside the tenant's RLS. Admission inserts the
shell, the run and the section; completion inserts the section version, the document version
and its link, and the verification rows. State columns are updated from the domain entities the
three machines already moved (``save_state``), so the transitions are validated in the domain
and guarded again by the database triggers (AS-5).
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from rag.domain.generation.artefact import Artefact
from rag.domain.generation.repositories import CompletionRows, IdempotencyRace
from rag.domain.generation.run import GenerationRun
from rag.domain.generation.section import ArtefactSection
from rag.domain.shared.enums import (
    ArtefactOrigin,
    CoherenceVerdict,
    EligibilityState,
    GenerationMode,
    OutlineState,
    RunFailureReason,
    RunStage,
    RunStatus,
)
from rag.infrastructure.persistence.generation.rows import dump_json, load_json

type Conn = Any


def _run(r: Any) -> GenerationRun:
    """``generation_runs`` row."""
    return GenerationRun(
        id=r["id"],
        artefact_id=r["artefact_id"],
        tenant_id=r["tenant_id"],
        principal_id=r["principal_id"],
        tender_id=r["tender_id"],
        origin=ArtefactOrigin(r["origin"]),
        base_version_id=r["base_version_id"],
        workflow_id=r["workflow_id"],
        run_input=None if r["run_input"] is None else load_json(r["run_input"]),
        current_stage=None if r["current_stage"] is None else RunStage(r["current_stage"]),
        idempotency_key=r["idempotency_key"],
        request_hash=r["request_hash"],
        status=RunStatus(r["status"]),
        failure_reason=None
        if r["failure_reason"] is None
        else RunFailureReason(r["failure_reason"]),
        failed_at_stage=None if r["failed_at_stage"] is None else RunStage(r["failed_at_stage"]),
        requested_mode=GenerationMode(r["requested_mode"]),
        effective_mode=GenerationMode(r["effective_mode"]),
        eligibility_state=EligibilityState(r["eligibility_state"]),
        clamped=r["clamped"],
        clamp_reason=r["clamp_reason"],
        domain_profile_version=r["domain_profile_version"],
        language_pack_version=r["language_pack_version"],
        outline_state=OutlineState(r["outline_state"]),
        outline_version=r["outline_version"],
        outline_ready_at=r["outline_ready_at"],
        gate_expires_at=r["gate_expires_at"],
        coherence=CoherenceVerdict(r["coherence"]),
        coherence_computed_at=r["coherence_computed_at"],
        record_version=r["record_version"],
        created_at=r["created_at"],
        updated_at=r["updated_at"],
    )


class PgGenerationWriteRepository:
    """``GenerationWriteRepository`` over asyncpg."""

    async def find_run_by_key(
        self, tx: Conn, tenant_id: UUID, principal_id: UUID, key: str
    ) -> GenerationRun | None:
        """The run holding the key (``uq_run_idempotency``)."""
        r = await tx.fetchrow(
            "SELECT * FROM generation_runs "
            "WHERE tenant_id = $1 AND principal_id = $2 AND idempotency_key = $3",
            tenant_id,
            principal_id,
            key,
        )
        return None if r is None else _run(r)

    async def release_key(self, tx: Conn, run_id: UUID) -> None:
        """NULL the key of a run that produced nothing (``ck_run_idem`` allows a NULL key)."""
        await tx.execute(
            "UPDATE generation_runs SET idempotency_key = NULL, request_hash = NULL WHERE id = $1",
            run_id,
        )

    async def insert_shell(
        self, tx: Conn, artefact: Artefact, run: GenerationRun, section: ArtefactSection | None
    ) -> None:
        """Artefact, run, section: one admission (RUN-1); a lost key race is ``IdempotencyRace``."""
        await tx.execute(
            "INSERT INTO artefacts (id, tenant_id, tender_id, kind, capability, "
            "tender_section_ref, target_ref, status, created_by) "
            "VALUES ($1, $2, $3, $4::artefact_kind, $5, $6, $7, $8::artefact_status, $9)",
            artefact.id,
            artefact.tenant_id,
            artefact.tender_id,
            artefact.kind.value,
            artefact.capability,
            artefact.tender_section_ref,
            artefact.target_ref,
            artefact.status.value,
            artefact.created_by,
        )
        await self.insert_run(tx, run)
        if section is None:
            return
        await tx.execute(
            "INSERT INTO artefact_sections (id, artefact_id, tenant_id, tender_section_ref, "
            "section_type, ordinal, is_foundation, state) "
            "VALUES ($1, $2, $3, $4, $5, $6, $7, $8::section_state)",
            section.id,
            section.artefact_id,
            section.tenant_id,
            section.tender_section_ref,
            section.section_type,
            section.ordinal,
            section.is_foundation,
            section.state.value,
        )

    async def insert_run(self, tx: Conn, run: GenerationRun) -> None:
        """One ``generation_runs`` row; a taken key or ``workflow_id`` is ``IdempotencyRace``."""
        inserted = await tx.fetchval(
            "INSERT INTO generation_runs (id, artefact_id, tenant_id, tender_id, principal_id, "
            "origin, base_version_id, workflow_id, run_input, current_stage, idempotency_key, "
            "request_hash, status, requested_mode, effective_mode, eligibility_state, clamped, "
            "clamp_reason, domain_profile_version, language_pack_version) "
            "VALUES ($1, $2, $3, $4, $5, $6::artefact_origin, $7, $8, $9::jsonb, "
            "$10::run_stage, $11, $12, $13::run_status, $14::generation_mode, "
            "$15::generation_mode, $16::eligibility_state, $17, $18, $19, $20) "
            "ON CONFLICT DO NOTHING RETURNING id",
            run.id,
            run.artefact_id,
            run.tenant_id,
            run.tender_id,
            run.principal_id,
            run.origin.value,
            run.base_version_id,
            run.workflow_id,
            None if run.run_input is None else dump_json(run.run_input),
            None if run.current_stage is None else run.current_stage.value,
            run.idempotency_key,
            run.request_hash,
            run.status.value,
            run.requested_mode.value,
            run.effective_mode.value,
            run.eligibility_state.value,
            run.clamped,
            run.clamp_reason,
            run.domain_profile_version,
            run.language_pack_version,
        )
        if inserted is None:
            raise IdempotencyRace

    async def save_state(
        self, tx: Conn, artefact: Artefact, run: GenerationRun, section: ArtefactSection
    ) -> None:
        """Write the columns the machines move."""
        await tx.execute(
            "UPDATE artefacts SET status = $2::artefact_status, current_version_id = $3 "
            "WHERE id = $1",
            artefact.id,
            artefact.status.value,
            artefact.current_version_id,
        )
        await tx.execute(
            "UPDATE generation_runs SET status = $2::run_status, "
            "failure_reason = $3::run_failure_reason, failed_at_stage = $4::run_stage, "
            "current_stage = $5::run_stage WHERE id = $1",
            run.id,
            run.status.value,
            None if run.failure_reason is None else run.failure_reason.value,
            None if run.failed_at_stage is None else run.failed_at_stage.value,
            None if run.current_stage is None else run.current_stage.value,
        )
        await tx.execute(
            "UPDATE artefact_sections SET state = $2::section_state, gap_reason = $3, "
            "failure_reason = $4::run_failure_reason, current_version_id = $5 WHERE id = $1",
            section.id,
            section.state.value,
            section.gap_reason,
            None if section.failure_reason is None else section.failure_reason.value,
            section.current_version_id,
        )

    async def save_run_and_section(
        self, tx: Conn, run: GenerationRun, section: ArtefactSection
    ) -> None:
        """The run and section columns the machines move; the artefact row is not written."""
        await tx.execute(
            "UPDATE generation_runs SET status = $2::run_status, "
            "failure_reason = $3::run_failure_reason, failed_at_stage = $4::run_stage, "
            "current_stage = $5::run_stage WHERE id = $1",
            run.id,
            run.status.value,
            None if run.failure_reason is None else run.failure_reason.value,
            None if run.failed_at_stage is None else run.failed_at_stage.value,
            None if run.current_stage is None else run.current_stage.value,
        )
        await tx.execute(
            "UPDATE artefact_sections SET state = $2::section_state, gap_reason = $3, "
            "failure_reason = $4::run_failure_reason, current_version_id = $5 WHERE id = $1",
            section.id,
            section.state.value,
            section.gap_reason,
            None if section.failure_reason is None else section.failure_reason.value,
            section.current_version_id,
        )

    async def next_version_numbers(
        self, tx: Conn, artefact_id: UUID, section_id: UUID
    ) -> tuple[int, int]:
        """``MAX + 1`` under a per-artefact transaction lock, so a gap never appears (§14.1)."""
        await tx.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended('av:' || $1::text, 0))", str(artefact_id)
        )
        version_no = await tx.fetchval(
            "SELECT COALESCE(MAX(version_no), 0) + 1 FROM artefact_versions WHERE artefact_id = $1",
            artefact_id,
        )
        section_no = await tx.fetchval(
            "SELECT COALESCE(MAX(version_no), 0) + 1 FROM artefact_section_versions "
            "WHERE section_id = $1",
            section_id,
        )
        return int(version_no), int(section_no)

    async def count_sections(self, tx: Conn, artefact_id: UUID) -> tuple[int, int]:
        """``(complete, total)``."""
        r = await tx.fetchrow(
            "SELECT count(*) FILTER (WHERE state = 'complete') AS done, count(*) AS total "
            "FROM artefact_sections WHERE artefact_id = $1",
            artefact_id,
        )
        return int(r["done"]), int(r["total"])

    async def insert_completion(self, tx: Conn, rows: CompletionRows) -> None:
        """Section version, document version, link, groundedness, claims, completeness."""
        sv, av = rows.section_version, rows.artefact_version
        await tx.execute(
            "INSERT INTO artefact_section_versions (id, section_id, tenant_id, version_no, "
            "origin, content_ref, content_sha256, retrieval_set_id, evidence_tier, completeness, "
            "scope_narrowed, model_profile_id, prompt_hash, serving_tier, auto_retry_count, "
            "manual_regen_count, degradation_markers) "
            "VALUES ($1, $2, $3, $4, $5::artefact_origin, $6, $7, $8, $9::evidence_tier, "
            "$10::completeness_verdict, $11, $12, $13, $14, $15, $16, $17::jsonb)",
            sv.id,
            sv.section_id,
            sv.tenant_id,
            sv.version_no,
            sv.origin.value,
            sv.content_ref,
            sv.content_sha256,
            sv.retrieval_set_id,
            sv.evidence_tier.value,
            sv.completeness.value,
            sv.scope_narrowed,
            sv.model_profile_id,
            sv.prompt_hash,
            sv.serving_tier,
            sv.auto_retry_count,
            sv.manual_regen_count,
            dump_json(sv.degradation_markers),
        )
        if av is not None:
            await tx.execute(
                "INSERT INTO artefact_versions (id, artefact_id, tenant_id, version_no, "
                "minted_reason, "
                "origin, evidence_tier, completeness, eligibility_state, requested_mode, "
                "effective_mode, clamped, clamp_reason, domain_profile_version, "
                "language_pack_version, "
                "model_profile_id, prompt_hash, parser_version, chunker_version, "
                "generation_run_id, "
                "output_schema_version, delivery, degradation_markers, instruction, "
                "instruction_refinement_id, review_context_read, review_context_ref, "
                "disclaimer_ref) "
                "VALUES ($1, $2, $3, $4, $5, $6::artefact_origin, $7::evidence_tier, "
                "$8::completeness_verdict, $9::eligibility_state, $10::generation_mode, "
                "$11::generation_mode, $12, $13, $14, $15, $16, $17, $18, $19, $20, $21, "
                "$22::delivery_state, $23::jsonb, $24, $25, $26, $27, $28)",
                av.id,
                av.artefact_id,
                av.tenant_id,
                av.version_no,
                av.minted_reason,
                av.origin.value,
                av.evidence_tier.value,
                av.completeness.value,
                av.eligibility_state.value,
                av.requested_mode.value,
                av.effective_mode.value,
                av.clamped,
                av.clamp_reason,
                av.domain_profile_version,
                av.language_pack_version,
                av.model_profile_id,
                av.prompt_hash,
                av.parser_version,
                av.chunker_version,
                av.generation_run_id,
                av.output_schema_version,
                av.delivery.value,
                dump_json(av.degradation_markers),
                av.instruction,
                av.instruction_refinement_id,
                av.review_context_read,
                av.review_context_ref,
                av.disclaimer_ref,
            )
            await tx.execute(
                "INSERT INTO artefact_version_sections (artefact_version_id, section_version_id, "
                "tenant_id) VALUES ($1, $2, $3)",
                av.id,
                sv.id,
                av.tenant_id,
            )
        if rows.groundedness is not None:
            g = rows.groundedness
            await tx.execute(
                "INSERT INTO groundedness_reports (id, section_version_id, tenant_id, tiers_run, "
                "claims_total, supported, unsupported, miscited, gaps, score, evidence_tier, "
                "judge_model_profile_id) "
                "VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11::evidence_tier, $12)",
                g.id,
                g.section_version_id,
                g.tenant_id,
                list(g.tiers_run),
                g.claims_total,
                g.supported,
                g.unsupported,
                g.miscited,
                g.gaps,
                g.score,
                g.evidence_tier.value,
                g.judge_model_profile_id,
            )
        if rows.claims:
            await tx.executemany(
                "INSERT INTO claim_verifications (report_id, claim_ordinal, tenant_id, outcome, "
                "cited_chunk_ids, corrected_chunk_ids, tier_decided) "
                "VALUES ($1, $2, $3, $4::claim_outcome, $5, $6, $7)",
                [
                    (
                        c.report_id,
                        c.claim_ordinal,
                        c.tenant_id,
                        c.outcome.value,
                        list(c.cited_chunk_ids),
                        None if c.corrected_chunk_ids is None else list(c.corrected_chunk_ids),
                        c.tier_decided,
                    )
                    for c in rows.claims
                ],
            )
        if rows.completeness is not None:
            c = rows.completeness
            await tx.execute(
                "INSERT INTO completeness_checks (id, section_version_id, tenant_id, verdict, "
                "items_total, items_addressed, missed_items) "
                "VALUES ($1, $2, $3, $4::completeness_verdict, $5, $6, $7)",
                c.id,
                c.section_version_id,
                c.tenant_id,
                c.verdict.value,
                c.items_total,
                c.items_addressed,
                list(c.missed_items),
            )
