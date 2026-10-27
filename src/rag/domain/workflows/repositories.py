"""Repository ports of the workflows module. ``tx`` is the opaque transaction handle."""

from __future__ import annotations

from typing import Any, Protocol
from uuid import UUID

from rag.domain.generation.artefact import Artefact
from rag.domain.generation.run import GenerationRun
from rag.domain.shared.enums import RunFailureReason, RunStatus
from rag.domain.workflows.views import RunStateView

type Tx = Any


class RunRepository(Protocol):
    """Run rows for the gate, cancel, resume and rematerialise routes and W-2."""

    async def get_run(self, tx: Tx, run_id: UUID, *, lock: bool = False) -> GenerationRun | None:
        """The run, or ``None`` (also another tenant's, RLS). ``lock`` takes ``FOR UPDATE``."""
        ...

    async def find_run_by_workflow_id(self, tx: Tx, workflow_id: str) -> GenerationRun | None:
        """The run holding ``workflow_id`` (RUN-2), or ``None``."""
        ...

    async def lock_workflow_id(self, tx: Tx, workflow_id: str) -> None:
        """Transaction-scoped advisory lock keyed on ``workflow_id``.

        Concurrent admissions of one regeneration take turns, so the second sees the first's row.
        """
        ...

    async def save_gate(
        self,
        tx: Tx,
        run: GenerationRun,
        *,
        expected_status: RunStatus,
        expected_record_version: int,
    ) -> int | None:
        """One guarded ``UPDATE`` of the status, outline and failure columns.

        ``WHERE id AND status = expected AND record_version = expected``: zero rows is ``None``
        (the caller answers ``409``). Returns the new ``record_version``, which orders
        ``GenerationProgressed`` as ``progress_seq`` (the trigger bumps it on every write).
        """
        ...

    async def get_artefact(
        self, tx: Tx, artefact_id: UUID, *, lock: bool = False
    ) -> Artefact | None:
        """The artefact row, optionally ``FOR UPDATE``."""
        ...

    async def save_artefact_status(self, tx: Tx, artefact: Artefact) -> None:
        """Persist ``status`` after a machine moved it (ART-5, ART-6)."""
        ...

    async def close_in_flight_sections(
        self, tx: Tx, artefact_id: UUID, failure: RunFailureReason
    ) -> int:
        """AS-8, set-based: close the artefact's ``generating`` sections.

        They become ``complete`` where a current version exists, else ``failed`` with ``failure``.

        ``pending`` and settled sections are untouched.
        """
        ...

    async def count_sections(self, tx: Tx, artefact_id: UUID) -> tuple[int, int]:
        """``(complete, total)`` for the events' ``sections_complete`` / ``sections_total``."""
        ...

    async def read_state(self, tx: Tx, run_id: UUID) -> RunStateView | None:
        """W-2's composite from ``generation_runs``, ``artefact_sections``, ``document_facts``."""
        ...
