"""SQL behind A-5, A-6 and A-7: section lookup and lock, facts, conflicts, refinement rows."""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from rag.domain.generation.artefact import ArtefactVersion
from rag.domain.generation.facts import DocumentFact, FactConflict
from rag.domain.generation.instruction import InstructionRefinement
from rag.domain.generation.section import ArtefactSection, SectionVersion
from rag.domain.shared.enums import ConflictResolution
from rag.infrastructure.persistence.generation import rows
from rag.infrastructure.persistence.generation.workflows import fact_from_row

type Conn = Any


def _conflict(r: Any) -> FactConflict:
    resolution = r["resolution"]
    return FactConflict(
        id=r["id"],
        tenant_id=r["tenant_id"],
        artefact_id=r["artefact_id"],
        section_id=r["section_id"],
        fact_key=r["fact_key"],
        established_value=r["established_value"],
        instruction_value=r["instruction_value"],
        established_by_section_id=r["established_by_section_id"],
        resolution=None if resolution is None else ConflictResolution(resolution),
        resolved_at=r["resolved_at"],
        record_version=r["record_version"],
        created_at=r["created_at"],
        updated_at=r["updated_at"],
    )


class PgRegenerationRepository:
    """``RegenerationRepository`` over asyncpg."""

    async def get_section(
        self, tx: Conn, artefact_id: UUID, ref: str
    ) -> tuple[ArtefactSection, SectionVersion | None] | None:
        """The section by ``tender_section_ref`` or id, with its current section version."""
        s = await tx.fetchrow(
            "SELECT * FROM artefact_sections WHERE artefact_id = $1 "
            "AND (tender_section_ref = $2 OR id::text = $2) ORDER BY ordinal LIMIT 1",
            artefact_id,
            ref,
        )
        if s is None:
            return None
        sv = None
        if s["current_version_id"] is not None:
            sv = await tx.fetchrow(
                "SELECT * FROM artefact_section_versions WHERE id = $1", s["current_version_id"]
            )
        return rows.section(s), None if sv is None else rows.section_version(sv)

    async def lock_section(self, tx: Conn, section_id: UUID) -> ArtefactSection | None:
        """``FOR UPDATE``."""
        r = await tx.fetchrow(
            "SELECT * FROM artefact_sections WHERE id = $1 FOR UPDATE", section_id
        )
        return None if r is None else rows.section(r)

    async def document_holds_section_version(
        self, tx: Conn, artefact_version_id: UUID, section_version_id: UUID
    ) -> bool:
        """The roll-up link (``artefact_version_sections``)."""
        return bool(
            await tx.fetchval(
                "SELECT EXISTS (SELECT 1 FROM artefact_version_sections "
                "WHERE artefact_version_id = $1 AND section_version_id = $2)",
                artefact_version_id,
                section_version_id,
            )
        )

    async def get_version(self, tx: Conn, artefact_version_id: UUID) -> ArtefactVersion | None:
        """A document version row."""
        r = await tx.fetchrow("SELECT * FROM artefact_versions WHERE id = $1", artefact_version_id)
        return None if r is None else rows.artefact_version(r)


class PgFactRepository:
    """``FactRepository`` over ``document_facts`` and ``fact_conflicts``."""

    async def current_facts(self, tx: Conn, artefact_id: UUID) -> list[DocumentFact]:
        """Current facts by key."""
        found = await tx.fetch(
            "SELECT * FROM document_facts WHERE artefact_id = $1 AND superseded_at IS NULL "
            "ORDER BY fact_key",
            artefact_id,
        )
        return [fact_from_row(r) for r in found]

    async def insert_conflict(self, tx: Conn, conflict: FactConflict) -> None:
        """A surfaced conflict."""
        await tx.execute(
            "INSERT INTO fact_conflicts (id, tenant_id, artefact_id, section_id, fact_key, "
            "established_value, instruction_value, established_by_section_id) "
            "VALUES ($1, $2, $3, $4, $5, $6, $7, $8)",
            conflict.id,
            conflict.tenant_id,
            conflict.artefact_id,
            conflict.section_id,
            conflict.fact_key,
            conflict.established_value,
            conflict.instruction_value,
            conflict.established_by_section_id,
        )

    async def get_conflict(
        self, tx: Conn, conflict_id: UUID, *, lock: bool = False
    ) -> FactConflict | None:
        """The conflict row."""
        r = await tx.fetchrow(
            "SELECT * FROM fact_conflicts WHERE id = $1" + (" FOR UPDATE" if lock else ""),
            conflict_id,
        )
        return None if r is None else _conflict(r)

    async def find_resolved(
        self, tx: Conn, section_id: UUID, fact_key: str, instruction_value: str
    ) -> FactConflict | None:
        """The latest resolved conflict of this section, key and value."""
        r = await tx.fetchrow(
            "SELECT * FROM fact_conflicts WHERE section_id = $1 AND fact_key = $2 "
            "AND instruction_value = $3 AND resolution IS NOT NULL "
            "ORDER BY resolved_at DESC LIMIT 1",
            section_id,
            fact_key,
            instruction_value,
        )
        return None if r is None else _conflict(r)

    async def save_resolution(self, tx: Conn, conflict: FactConflict) -> bool:
        """Guarded on the conflict's own state: ``resolution IS NULL`` (§5.7.10)."""
        assert conflict.resolution is not None
        tag = await tx.execute(
            "UPDATE fact_conflicts SET resolution = $2::conflict_resolution, resolved_at = $3 "
            "WHERE id = $1 AND resolution IS NULL",
            conflict.id,
            conflict.resolution.value,
            conflict.resolved_at,
        )
        return bool(tag.endswith(" 1"))

    async def supersede_fact(
        self, tx: Conn, artefact_id: UUID, fact_key: str, at: datetime
    ) -> bool:
        """``superseded_at`` is the one column ``trg_df_immutable`` lets change."""
        tag = await tx.execute(
            "UPDATE document_facts SET superseded_at = $3 "
            "WHERE artefact_id = $1 AND fact_key = $2 AND superseded_at IS NULL",
            artefact_id,
            fact_key,
            at,
        )
        return bool(tag.endswith(" 1"))

    async def insert_fact(self, tx: Conn, fact: DocumentFact) -> None:
        """Append the new current fact."""
        await tx.execute(
            "INSERT INTO document_facts (id, artefact_id, tenant_id, fact_key, value, "
            "established_by_section_id) VALUES ($1, $2, $3, $4, $5, $6)",
            fact.id,
            fact.artefact_id,
            fact.tenant_id,
            fact.fact_key,
            fact.value,
            fact.established_by_section_id,
        )

    async def flag_sections(
        self, tx: Conn, artefact_id: UUID, *, except_section_id: UUID, reason: str
    ) -> list[UUID]:
        """``flagged_for_review`` on every other section that has a version (never regenerates)."""
        found = await tx.fetch(
            "UPDATE artefact_sections SET flagged_for_review = true, flagged_reason = $3 "
            "WHERE artefact_id = $1 AND id <> $2 AND current_version_id IS NOT NULL "
            "AND NOT flagged_for_review RETURNING id",
            artefact_id,
            except_section_id,
            reason,
        )
        return [r["id"] for r in found]


class PgInstructionRefinementRepository:
    """``InstructionRefinementRepository`` over ``instruction_refinements``."""

    async def insert(self, tx: Conn, row: InstructionRefinement) -> None:
        """One row per refiner call that ran (REF-1)."""
        await tx.execute(
            "INSERT INTO instruction_refinements (id, tenant_id, artefact_version_id, section_id, "
            "principal_hash, original_instruction, refined_instruction, instruction_type, "
            "needs_retrieval, fact_conflict_id, conflict_resolution, pack_version, "
            "refiner_version, latency_ms, outcome, user_action) "
            "VALUES ($1, $2, $3, $4, $5, $6, $7, $8::instruction_type, $9, $10, "
            "$11::conflict_resolution, $12, $13, $14, $15::refiner_outcome, $16::user_action)",
            row.id,
            row.tenant_id,
            row.artefact_version_id,
            row.section_id,
            row.principal_hash,
            row.original_instruction,
            row.refined_instruction,
            None if row.instruction_type is None else row.instruction_type.value,
            row.needs_retrieval,
            row.fact_conflict_id,
            None if row.conflict_resolution is None else row.conflict_resolution.value,
            row.pack_version,
            row.refiner_version,
            row.latency_ms,
            row.outcome.value,
            row.user_action.value,
        )

    async def record_resolution(self, tx: Conn, fact_conflict_id: UUID, resolution: str) -> None:
        """The refinement that surfaced the conflict learns how it was resolved."""
        await tx.execute(
            "UPDATE instruction_refinements SET conflict_resolution = $2::conflict_resolution "
            "WHERE fact_conflict_id = $1",
            fact_conflict_id,
            resolution,
        )
