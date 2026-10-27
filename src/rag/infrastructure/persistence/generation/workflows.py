"""SQL of the workflows module: gate transitions, cancel and W-2 (LLD §5.7.13, §5.7.14, §14.1).

Runs on the connection ``run_in_tx`` hands over, inside the tenant's RLS. Gate transitions are
one guarded ``UPDATE`` (``WHERE status = expected AND record_version = expected``): zero rows is
a ``409``, never a read-check-write race. ``record_version`` is bumped by the table's trigger on
every write, so the value returned orders ``GenerationProgressed`` as ``progress_seq`` (EVT-13).
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from rag.domain.generation.artefact import Artefact
from rag.domain.generation.facts import DocumentFact
from rag.domain.generation.run import GenerationRun
from rag.domain.shared.enums import EvidenceTier, RunFailureReason, RunStatus
from rag.domain.workflows.views import RunStateView, SectionProgress
from rag.infrastructure.persistence.generation import rows
from rag.infrastructure.persistence.generation.writes import _run as run_row

type Conn = Any


def fact_from_row(r: Any) -> DocumentFact:
    """``document_facts`` row."""
    return DocumentFact(
        id=r["id"],
        artefact_id=r["artefact_id"],
        tenant_id=r["tenant_id"],
        fact_key=r["fact_key"],
        value=r["value"],
        established_by_section_id=r["established_by_section_id"],
        superseded_at=r["superseded_at"],
        created_at=r["created_at"],
    )


class PgRunRepository:
    """``RunRepository`` over asyncpg."""

    async def get_run(self, tx: Conn, run_id: UUID, *, lock: bool = False) -> GenerationRun | None:
        """The run row, ``FOR UPDATE`` when ``lock``."""
        r = await tx.fetchrow(
            "SELECT * FROM generation_runs WHERE id = $1" + (" FOR UPDATE" if lock else ""), run_id
        )
        return None if r is None else run_row(r)

    async def find_run_by_workflow_id(self, tx: Conn, workflow_id: str) -> GenerationRun | None:
        """The run holding ``workflow_id`` (``uq_run_workflow``)."""
        r = await tx.fetchrow("SELECT * FROM generation_runs WHERE workflow_id = $1", workflow_id)
        return None if r is None else run_row(r)

    async def lock_workflow_id(self, tx: Conn, workflow_id: str) -> None:
        """Serialise admissions of one ``workflow_id`` (taken to commit)."""
        await tx.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended('wf:' || $1::text, 0))", workflow_id
        )

    async def save_gate(
        self,
        tx: Conn,
        run: GenerationRun,
        *,
        expected_status: RunStatus,
        expected_record_version: int,
    ) -> int | None:
        """One guarded update; ``None`` where the run is no longer as the caller read it."""
        version = await tx.fetchval(
            "UPDATE generation_runs SET status = $4::run_status, "
            "outline_state = $5::outline_state, outline_version = $6, "
            "current_stage = $7::run_stage, failure_reason = $8::run_failure_reason, "
            "failed_at_stage = $9::run_stage "
            "WHERE id = $1 AND status = $2::run_status AND record_version = $3 "
            "RETURNING record_version",
            run.id,
            expected_status.value,
            expected_record_version,
            run.status.value,
            run.outline_state.value,
            run.outline_version,
            None if run.current_stage is None else run.current_stage.value,
            None if run.failure_reason is None else run.failure_reason.value,
            None if run.failed_at_stage is None else run.failed_at_stage.value,
        )
        return None if version is None else int(version)

    async def get_artefact(
        self, tx: Conn, artefact_id: UUID, *, lock: bool = False
    ) -> Artefact | None:
        """The artefact row, ``FOR UPDATE`` when ``lock``."""
        r = await tx.fetchrow(
            "SELECT * FROM artefacts WHERE id = $1" + (" FOR UPDATE" if lock else ""), artefact_id
        )
        return None if r is None else rows.artefact(r)

    async def save_artefact_status(self, tx: Conn, artefact: Artefact) -> None:
        """Persist the status the ART-5 machine moved."""
        await tx.execute(
            "UPDATE artefacts SET status = $2::artefact_status WHERE id = $1",
            artefact.id,
            artefact.status.value,
        )

    async def close_in_flight_sections(
        self, tx: Conn, artefact_id: UUID, failure: RunFailureReason
    ) -> int:
        """AS-8: ``generating`` -> ``complete`` (a version exists) or ``failed`` (none)."""
        tag = await tx.execute(
            "UPDATE artefact_sections SET "
            "state = CASE WHEN current_version_id IS NOT NULL THEN 'complete' ELSE 'failed' END"
            "::section_state, "
            "failure_reason = CASE WHEN current_version_id IS NULL THEN $2::run_failure_reason END "
            "WHERE artefact_id = $1 AND state = 'generating'",
            artefact_id,
            failure.value,
        )
        return int(tag.rsplit(" ", 1)[-1])

    async def count_sections(self, tx: Conn, artefact_id: UUID) -> tuple[int, int]:
        """``(complete, total)``."""
        r = await tx.fetchrow(
            "SELECT count(*) FILTER (WHERE state = 'complete') AS done, count(*) AS total "
            "FROM artefact_sections WHERE artefact_id = $1",
            artefact_id,
        )
        return int(r["done"]), int(r["total"])

    async def read_state(self, tx: Conn, run_id: UUID) -> RunStateView | None:
        """W-2's composite: the run, the artefact, the sections with their tiers, current facts."""
        run_r = await tx.fetchrow("SELECT * FROM generation_runs WHERE id = $1", run_id)
        if run_r is None:
            return None
        art_r = await tx.fetchrow("SELECT * FROM artefacts WHERE id = $1", run_r["artefact_id"])
        if art_r is None:
            return None
        section_rows = await tx.fetch(
            "SELECT s.*, v.evidence_tier AS v_tier, v.retrieval_set_id AS v_set "
            "FROM artefact_sections s "
            "LEFT JOIN artefact_section_versions v ON v.id = s.current_version_id "
            "WHERE s.artefact_id = $1 ORDER BY s.ordinal",
            run_r["artefact_id"],
        )
        fact_rows = await tx.fetch(
            "SELECT * FROM document_facts WHERE artefact_id = $1 AND superseded_at IS NULL "
            "ORDER BY fact_key",
            run_r["artefact_id"],
        )
        narrowed = await tx.fetchval(
            "SELECT EXISTS (SELECT 1 FROM artefact_section_versions v "
            "JOIN artefact_sections s ON s.id = v.section_id "
            "WHERE s.artefact_id = $1 AND v.scope_narrowed)",
            run_r["artefact_id"],
        )
        return RunStateView(
            run=run_row(run_r),
            artefact=rows.artefact(art_r),
            sections=tuple(
                SectionProgress(
                    rows.section(r),
                    None if r["v_tier"] is None else EvidenceTier(r["v_tier"]),
                    r["v_set"],
                )
                for r in section_rows
            ),
            facts=tuple(fact_from_row(r) for r in fact_rows),
            scope_narrowed=bool(narrowed),
        )
