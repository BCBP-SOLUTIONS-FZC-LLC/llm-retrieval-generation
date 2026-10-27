"""asyncpg implementations of the archive, sweep and quota-consumer statements.

Like the other repositories they run on the connection of the caller's transaction, inside the
tenant's RLS: a sweep binds one tenant first, so none of these needs a ``tenant_id`` predicate.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from rag.application.events.ports import SuspendedRun
from rag.domain.generation.ops import (
    ArchivableArtefact,
    FailedArtefact,
    StuckArtefact,
)
from rag.infrastructure.persistence.generation.rows import load_json

type Conn = Any

_LIVE_RUN = "('pending','outline_building','awaiting_outline_approval','generating','suspended')"
_BATCH = 200


def _count(tag: str) -> int:
    return int(tag.rsplit(" ", 1)[-1])


def _stuck(r: Any) -> StuckArtefact:
    return StuckArtefact(
        r["id"],
        r["tender_id"],
        r["run_id"],
        r["principal_id"],
        r["workflow_id"],
        None if r["run_input"] is None else load_json(r["run_input"]),
    )


_STUCK_SELECT = (
    "SELECT a.id, a.tender_id, r.id AS run_id, r.principal_id, r.workflow_id, r.run_input "
    "FROM artefacts a LEFT JOIN LATERAL (SELECT * FROM generation_runs g "
    "WHERE g.artefact_id = a.id ORDER BY g.created_at DESC, g.id DESC LIMIT 1) r ON true "
)


class PgArtefactArchiveRepository:
    """``ArtefactArchiveRepository`` (ART-7)."""

    async def list_live_for_tender(
        self, tx: Conn, tenant_id: UUID, tender_id: UUID
    ) -> list[ArchivableArtefact]:
        """Live artefacts of the tender, locked."""
        rows = await tx.fetch(
            "SELECT id, tender_id FROM artefacts WHERE tenant_id = $1 AND tender_id = $2 "
            "AND archive_state = 'live' ORDER BY id FOR UPDATE",
            tenant_id,
            tender_id,
        )
        return [ArchivableArtefact(r["id"], r["tender_id"]) for r in rows]

    async def mark_archived(self, tx: Conn, artefact_id: UUID, at: datetime) -> bool:
        """``live`` -> ``archived`` once (``ck_artefact_archive`` needs ``archived_at``)."""
        tag = await tx.execute(
            "UPDATE artefacts SET archive_state = 'archived', archived_at = $2 "
            "WHERE id = $1 AND archive_state = 'live' AND tender_id IS NOT NULL",
            artefact_id,
            at,
        )
        return _count(tag) == 1


class PgSweepRepository:
    """``SweepRepository`` (§21.2)."""

    async def pending_artefacts(self, tx: Conn, before: datetime) -> list[StuckArtefact]:
        """Pending artefacts with their latest run (``idx_artefacts_pending``)."""
        rows = await tx.fetch(
            _STUCK_SELECT + "WHERE a.status = 'pending' AND a.created_at < $1 "
            f"ORDER BY a.created_at LIMIT {_BATCH}",
            before,
        )
        return [_stuck(r) for r in rows]

    async def stalled_artefacts(self, tx: Conn, before: datetime) -> list[StuckArtefact]:
        """Generating artefacts untouched since ``before`` whose runs are all quiet too."""
        rows = await tx.fetch(
            _STUCK_SELECT + "WHERE a.status = 'generating' AND a.updated_at < $1 "
            "AND NOT EXISTS (SELECT 1 FROM generation_runs l WHERE l.artefact_id = a.id "
            f"AND l.status IN {_LIVE_RUN} AND l.updated_at >= $1) "
            f"ORDER BY a.updated_at LIMIT {_BATCH}",
            before,
        )
        return [_stuck(r) for r in rows]

    async def fail_artefact(
        self, tx: Conn, artefact_id: UUID, now: datetime
    ) -> FailedArtefact | None:
        """Close the artefact and its live run as ``failed`` / ``internal`` (RUN-5).

        Written as SQL because the domain's run machine has no ``pending -> failed`` edge, which
        §21.2's orphan sweep needs; the LLD names the transition explicitly.
        """
        a = await tx.fetchrow(
            "SELECT id, tenant_id, tender_id, created_by FROM artefacts "
            "WHERE id = $1 AND status IN ('pending','generating') FOR UPDATE",
            artefact_id,
        )
        if a is None:
            return None
        run = await tx.fetchrow(
            "SELECT id, principal_id, current_stage, status FROM generation_runs "
            "WHERE artefact_id = $1 ORDER BY created_at DESC, id DESC LIMIT 1 FOR UPDATE",
            artefact_id,
        )
        stage = None
        if run is not None and run["status"] in {
            "pending",
            "outline_building",
            "awaiting_outline_approval",
            "generating",
            "suspended",
        }:
            stage = run["current_stage"]
            await tx.execute(
                "UPDATE generation_runs SET status = 'failed', failure_reason = 'internal', "
                "failed_at_stage = current_stage WHERE id = $1",
                run["id"],
            )
        await tx.execute("UPDATE artefacts SET status = 'failed' WHERE id = $1", artefact_id)
        counts = await tx.fetchrow(
            "SELECT count(*) FILTER (WHERE state = 'complete') AS done, count(*) AS total "
            "FROM artefact_sections WHERE artefact_id = $1",
            artefact_id,
        )
        return FailedArtefact(
            artefact_id=artefact_id,
            tenant_id=a["tenant_id"],
            tender_id=a["tender_id"],
            run_id=None if run is None else run["id"],
            principal_id=a["created_by"] if run is None else run["principal_id"],
            failed_at_stage=None if stage is None else str(stage),
            sections_complete=counts["done"],
            sections_total=counts["total"],
        )

    async def clear_idempotency_keys(self, tx: Conn, before: datetime) -> int:
        """NULL the key; the run row stays (RUN-3)."""
        return _count(
            await tx.execute(
                "UPDATE generation_runs SET idempotency_key = NULL "
                "WHERE idempotency_key IS NOT NULL AND created_at < $1",
                before,
            )
        )

    async def delete_orphan_retrieval_sets(self, tx: Conn, before: datetime, limit: int) -> int:
        """Unreferenced sets with their chunks and markers.

        Refinement rows are *kept*: they are the planned golden-set source (§21.1), so they lose
        their pointer, not their text.
        """
        rows = await tx.fetch(
            "SELECT id FROM retrieval_sets rs WHERE rs.artefact_id IS NULL AND rs.created_at < $1 "
            "AND NOT EXISTS (SELECT 1 FROM artefact_section_versions v "
            "WHERE v.retrieval_set_id = rs.id) "
            "AND NOT EXISTS (SELECT 1 FROM retrieval_sets w WHERE w.widened_retry_of = rs.id) "
            "ORDER BY rs.created_at LIMIT $2",
            before,
            limit,
        )
        ids = [r["id"] for r in rows]
        if not ids:
            return 0
        await tx.execute(
            "UPDATE query_refinements SET retrieval_set_id = NULL "
            "WHERE retrieval_set_id = ANY($1::uuid[])",
            ids,
        )
        for table in ("retrieval_set_purges", "retrieval_set_chunks"):
            await tx.execute(f"DELETE FROM {table} WHERE retrieval_set_id = ANY($1::uuid[])", ids)
        await tx.execute("DELETE FROM retrieval_sets WHERE id = ANY($1::uuid[])", ids)
        return len(ids)

    async def delete_stale_checkpoints(self, tx: Conn, before: datetime) -> int:
        """Checkpoints older than ``before`` whose section is not generating any more (GC-1)."""
        return _count(
            await tx.execute(
                "DELETE FROM graph_checkpoints c WHERE c.created_at < $1 AND NOT EXISTS ("
                "SELECT 1 FROM artefact_sections s WHERE s.id = c.section_id "
                "AND s.state = 'generating')",
                before,
            )
        )


class PgRunDirectory:
    """``RunDirectory`` for the ``quota_resume`` consumer."""

    async def suspended_on_quota(
        self, tx: Conn, *, after: UUID | None, limit: int
    ) -> list[SuspendedRun]:
        """Suspended runs of the bound tenant whose reason is ``quota_exhausted`` (LD-70)."""
        rows = await tx.fetch(
            "SELECT id, artefact_id, principal_id FROM generation_runs "
            "WHERE status = 'suspended' AND failure_reason = 'quota_exhausted' "
            "AND ($1::uuid IS NULL OR id > $1) ORDER BY id LIMIT $2",
            after,
            limit,
        )
        return [SuspendedRun(r["id"], r["artefact_id"], r["principal_id"]) for r in rows]
