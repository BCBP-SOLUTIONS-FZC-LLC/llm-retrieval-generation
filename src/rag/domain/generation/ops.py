"""Ports for the archive consumer (ART-7) and the scheduled sweeps (§21.2) over ``llm_generation``.

Every method runs on the transaction handle of the caller, inside the tenant's RLS: a sweep binds
one tenant, then calls these, so each statement is already scoped to it.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol
from uuid import UUID

type Tx = Any


@dataclass(frozen=True, slots=True)
class ArchivableArtefact:
    """A live artefact of a closing tender."""

    id: UUID
    tender_id: UUID


@dataclass(frozen=True, slots=True)
class StuckArtefact:
    """An artefact a sweep found stuck, with its latest run (``None`` when it has none)."""

    artefact_id: UUID
    tender_id: UUID | None
    run_id: UUID | None
    principal_id: UUID | None
    workflow_id: str | None
    run_input: dict[str, Any] | None


@dataclass(frozen=True, slots=True)
class FailedArtefact:
    """What ``fail_artefact`` closed: the figures ``GenerationFailed`` carries."""

    artefact_id: UUID
    tenant_id: UUID
    tender_id: UUID | None
    run_id: UUID | None
    principal_id: UUID
    failed_at_stage: str | None
    sections_complete: int
    sections_total: int


class ArtefactArchiveRepository(Protocol):
    """ART-7: the tender's artefacts become read-only."""

    async def list_live_for_tender(
        self, tx: Tx, tenant_id: UUID, tender_id: UUID
    ) -> list[ArchivableArtefact]:
        """Artefacts of the tender whose ``archive_state`` is still ``live``."""
        ...

    async def mark_archived(self, tx: Tx, artefact_id: UUID, at: datetime) -> bool:
        """``archive_state = archived`` where still live; ``False`` when it already was."""
        ...


class SweepRepository(Protocol):
    """The statements of the generation-side sweeps (§21.2)."""

    async def pending_artefacts(self, tx: Tx, before: datetime) -> list[StuckArtefact]:
        """``status = 'pending'`` and created before ``before`` (``idx_artefacts_pending``)."""
        ...

    async def stalled_artefacts(self, tx: Tx, before: datetime) -> list[StuckArtefact]:
        """``status = 'generating'``, not updated since ``before``, with no live run."""
        ...

    async def fail_artefact(
        self, tx: Tx, artefact_id: UUID, now: datetime
    ) -> FailedArtefact | None:
        """Artefact and its non-terminal run to ``failed`` / ``internal``; ``None`` if it moved."""
        ...

    async def clear_idempotency_keys(self, tx: Tx, before: datetime) -> int:
        """Set ``idempotency_key`` NULL on runs created before ``before``; rows changed (RUN-3)."""
        ...

    async def delete_orphan_retrieval_sets(self, tx: Tx, before: datetime, limit: int) -> int:
        """Delete sets with no artefact, no section version, created before ``before``."""
        ...

    async def delete_stale_checkpoints(self, tx: Tx, before: datetime) -> int:
        """Delete checkpoints created before ``before`` whose section is no longer generating."""
        ...
