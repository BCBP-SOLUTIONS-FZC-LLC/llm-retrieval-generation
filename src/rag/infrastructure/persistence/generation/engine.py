"""SQL behind the engine's writes (workers LLD §4.2) that no other repository owns.

Every statement runs as ``rag_activity`` in the tenant's RLS, inside the caller's transaction. The
section rows of an outline, the outline timing, the suspension stamp, the coherence verdict, the
roll-up read, the checkpoint row and the stored instruction have no other home: the API service
never writes them.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID

from rag.domain.generation.section import ArtefactSection
from rag.domain.shared.enums import CoherenceVerdict, EvidenceTier
from rag.infrastructure.persistence.generation import rows
from rag.infrastructure.persistence.generation.rows import dump_json

type Conn = Any


@dataclass(frozen=True, slots=True)
class OutlineEntry:
    """One section of an outline: a reference, a type and whether stage 1 owns it."""

    ref: str
    section_type: str
    is_foundation: bool = False


@dataclass(frozen=True, slots=True)
class CurrentSectionVersion:
    """A section's current version, as the document roll-up reads it (AV-3)."""

    section_id: UUID
    section_version_id: UUID
    evidence_tier: EvidenceTier
    content_sha256: bytes
    model_profile_id: str
    prompt_hash: str


class PgEngineRepository:
    """``EngineRepository`` over ``llm_generation``."""

    async def list_sections(self, tx: Conn, artefact_id: UUID) -> list[ArtefactSection]:
        """The artefact's sections in outline order."""
        found = await tx.fetch(
            "SELECT * FROM artefact_sections WHERE artefact_id = $1 ORDER BY ordinal, id",
            artefact_id,
        )
        return [rows.section(r) for r in found]

    async def get_section(
        self, tx: Conn, section_id: UUID, *, lock: bool = False
    ) -> ArtefactSection | None:
        """One section, ``FOR UPDATE`` when ``lock``."""
        r = await tx.fetchrow(
            "SELECT * FROM artefact_sections WHERE id = $1" + (" FOR UPDATE" if lock else ""),
            section_id,
        )
        return None if r is None else rows.section(r)

    async def current_content(self, tx: Conn, section_id: UUID) -> tuple[str, bytes] | None:
        """``(content_ref, content_sha256)`` of the section's current version, else ``None``."""
        r = await tx.fetchrow(
            "SELECT v.content_ref, v.content_sha256 FROM artefact_sections s "
            "JOIN artefact_section_versions v ON v.id = s.current_version_id WHERE s.id = $1",
            section_id,
        )
        return None if r is None else (r["content_ref"], bytes(r["content_sha256"]))

    async def save_section_state(self, tx: Conn, section: ArtefactSection) -> None:
        """Persist what the section machine moved (AS-5, AS-7)."""
        await tx.execute(
            "UPDATE artefact_sections SET state = $2::section_state, gap_reason = $3, "
            "failure_reason = $4::run_failure_reason, current_version_id = $5 WHERE id = $1",
            section.id,
            section.state.value,
            section.gap_reason,
            None if section.failure_reason is None else section.failure_reason.value,
            section.current_version_id,
        )

    async def replace_outline(
        self, tx: Conn, artefact_id: UUID, tenant_id: UUID, entries: Sequence[OutlineEntry]
    ) -> list[ArtefactSection]:
        """Write the outline's rows. A revision replaces the rows that have not started.

        Only a ``pending`` section with no version can go (the trigger enforces it for a worker),
        so a restart cannot erase finished work.
        """
        await tx.execute(
            "DELETE FROM artefact_sections WHERE artefact_id = $1 AND state = 'pending' "
            "AND current_version_id IS NULL",
            artefact_id,
        )
        written: list[ArtefactSection] = []
        for ordinal, e in enumerate(entries, start=1):
            r = await tx.fetchrow(
                "INSERT INTO artefact_sections (artefact_id, tenant_id, tender_section_ref, "
                "section_type, ordinal, is_foundation) VALUES ($1, $2, $3, $4, $5, $6) RETURNING *",
                artefact_id,
                tenant_id,
                e.ref,
                e.section_type,
                ordinal,
                e.is_foundation,
            )
            written.append(rows.section(r))
        return written

    async def set_outline_timing(
        self, tx: Conn, run_id: UUID, ready_at: datetime, gate_expires_at: datetime
    ) -> None:
        """``outline_ready_at`` and ``gate_expires_at`` of a gated run (WP-15)."""
        await tx.execute(
            "UPDATE generation_runs SET outline_ready_at = $2, gate_expires_at = $3 WHERE id = $1",
            run_id,
            ready_at,
            gate_expires_at,
        )

    async def set_artefact_current_version(
        self, tx: Conn, artefact_id: UUID, version_id: UUID
    ) -> None:
        """The document version a completed run minted becomes the artefact's current one."""
        await tx.execute(
            "UPDATE artefacts SET current_version_id = $2 WHERE id = $1", artefact_id, version_id
        )

    async def section_done_in_run(
        self, tx: Conn, section_id: UUID, run_started_at: datetime
    ) -> bool:
        """Whether the section's current version was written by this run (a restart skips it).

        A regeneration's sections already hold their base version, so ``complete`` alone does not
        mean "done in this run" (ASYNC-14).
        """
        return bool(
            await tx.fetchval(
                "SELECT EXISTS (SELECT 1 FROM artefact_sections s JOIN artefact_section_versions v "
                "ON v.id = s.current_version_id WHERE s.id = $1 AND s.state = 'complete' "
                "AND v.created_at >= $2)",
                section_id,
                run_started_at,
            )
        )

    async def flag_section(self, tx: Conn, section_id: UUID, reason: str) -> None:
        """``flagged_for_review`` on one section (a coherence conflict names it)."""
        await tx.execute(
            "UPDATE artefact_sections SET flagged_for_review = true, flagged_reason = $2 "
            "WHERE id = $1",
            section_id,
            reason,
        )

    async def document_version_of_run(self, tx: Conn, run_id: UUID) -> tuple[UUID, int] | None:
        """The document version a run already minted (a repeat of ``mint_document_version``)."""
        r = await tx.fetchrow(
            "SELECT id, version_no FROM artefact_versions WHERE generation_run_id = $1", run_id
        )
        return None if r is None else (r["id"], int(r["version_no"]))

    async def suspended_at(self, tx: Conn, run_id: UUID) -> datetime | None:
        """When the run entered ``suspended``, or ``None``."""
        value = await tx.fetchval("SELECT suspended_at FROM generation_runs WHERE id = $1", run_id)
        return value  # type: ignore[no-any-return]

    async def mark_suspended(self, tx: Conn, run_id: UUID, at: datetime) -> None:
        """Stamp when the run entered ``suspended`` (workers LLD G-3)."""
        await tx.execute("UPDATE generation_runs SET suspended_at = $2 WHERE id = $1", run_id, at)

    async def set_coherence(
        self, tx: Conn, run_id: UUID, verdict: CoherenceVerdict, at: datetime
    ) -> None:
        """``coherence`` and ``coherence_computed_at``."""
        await tx.execute(
            "UPDATE generation_runs SET coherence = $2::coherence_verdict, "
            "coherence_computed_at = $3 WHERE id = $1",
            run_id,
            verdict.value,
            at,
        )

    async def current_section_versions(
        self, tx: Conn, artefact_id: UUID
    ) -> list[CurrentSectionVersion]:
        """The current version of every section that has one: the set a document rolls up over."""
        found = await tx.fetch(
            "SELECT s.id AS section_id, v.id AS section_version_id, v.evidence_tier, "
            "v.content_sha256, v.model_profile_id, v.prompt_hash "
            "FROM artefact_sections s "
            "JOIN artefact_section_versions v ON v.id = s.current_version_id "
            "WHERE s.artefact_id = $1 ORDER BY s.ordinal, s.id",
            artefact_id,
        )
        return [
            CurrentSectionVersion(
                r["section_id"],
                r["section_version_id"],
                EvidenceTier(r["evidence_tier"]),
                bytes(r["content_sha256"]),
                r["model_profile_id"],
                r["prompt_hash"],
            )
            for r in found
        ]

    async def section_outcomes(self, tx: Conn, artefact_id: UUID) -> tuple[int, int, int]:
        """``(failed, gap, total)`` over the artefact's sections."""
        r = await tx.fetchrow(
            "SELECT count(*) FILTER (WHERE state = 'failed') AS failed, "
            "count(*) FILTER (WHERE state = 'gap') AS gap, count(*) AS total "
            "FROM artefact_sections WHERE artefact_id = $1",
            artefact_id,
        )
        return int(r["failed"]), int(r["gap"]), int(r["total"])

    async def insert_checkpoint(
        self,
        tx: Conn,
        tenant_id: UUID,
        section_id: UUID,
        activity_run_id: str,
        state: dict[str, Any],
    ) -> None:
        """One inspection row: node names, timings and counts only (workers LLD G-12, WA-11)."""
        await tx.execute(
            "INSERT INTO graph_checkpoints (tenant_id, section_id, activity_run_id, state) "
            "VALUES ($1, $2, $3, $4::jsonb)",
            tenant_id,
            section_id,
            activity_run_id,
            dump_json(state),
        )

    async def stored_instruction(self, tx: Conn, run_id: UUID) -> str | None:
        """The reviewer's words of an A-5 run, read back from its stored input (P0-4)."""
        value = await tx.fetchval(
            "SELECT run_input->>'instruction' FROM generation_runs WHERE id = $1", run_id
        )
        return None if value is None else str(value)
