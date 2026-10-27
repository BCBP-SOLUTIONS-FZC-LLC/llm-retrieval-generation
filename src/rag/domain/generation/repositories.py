"""Repository ports of the generation module. ``tx`` is the opaque transaction handle."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol
from uuid import UUID

from rag.domain.generation.artefact import Artefact, ArtefactVersion
from rag.domain.generation.audit import AuditEntry, AuditEntryDraft
from rag.domain.generation.facts import DocumentFact, FactConflict
from rag.domain.generation.instruction import InstructionRefinement
from rag.domain.generation.refinement import QueryRefinement
from rag.domain.generation.retrieval import RetrievalSetView
from rag.domain.generation.run import GenerationRun
from rag.domain.generation.section import ArtefactSection, SectionVersion
from rag.domain.generation.verification import (
    ClaimVerification,
    CompletenessCheck,
    GroundednessReport,
)
from rag.domain.generation.views import (
    ArtefactView,
    SectionStateView,
    SectionVersionRecord,
    VersionDetail,
)

type Tx = Any


class ArtefactReadRepository(Protocol):
    """Reads behind A-1 to A-4 and A-9. Every method runs under the tenant's RLS."""

    async def get_artefact(self, tx: Tx, artefact_id: UUID) -> Artefact | None:
        """The artefact row, or ``None`` (also for another tenant's)."""
        ...

    async def get_artefact_view(self, tx: Tx, artefact_id: UUID) -> ArtefactView | None:
        """Artefact + current version + latest run id."""
        ...

    async def list_versions(
        self, tx: Tx, artefact_id: UUID, *, limit: int, offset: int
    ) -> tuple[list[ArtefactVersion], int]:
        """A page of versions, newest first, and the total count."""
        ...

    async def get_version_detail(
        self, tx: Tx, artefact_id: UUID, version_no: int
    ) -> VersionDetail | None:
        """One version with its section versions, groundedness, claims and completeness."""
        ...

    async def list_sections(
        self, tx: Tx, artefact_id: UUID, *, limit: int, offset: int
    ) -> tuple[list[SectionStateView], int]:
        """A page of sections by ordinal, and the total count."""
        ...

    async def get_section_version_record(
        self, tx: Tx, artefact_id: UUID, section_ref: str, version_no: int
    ) -> SectionVersionRecord | None:
        """A section version by the section's ``tender_section_ref`` (or id) and version number."""
        ...


class RetrievalSetRepository(Protocol):
    """The recorded retrieval set (R-5) and its purge markers (EVT-1)."""

    async def get_view(self, tx: Tx, retrieval_set_id: UUID) -> RetrievalSetView | None:
        """The set with ordered chunks and purge rows, or ``None``."""
        ...

    async def insert(self, tx: Tx, view: RetrievalSetView) -> None:
        """Write a new set with its ordered chunks in the caller's transaction (RS-1: once).

        Chunk rows carry the caller's ``tenant_id``, reference collections included (RLS).
        """
        ...

    async def purge_for_document(
        self,
        tx: Tx,
        *,
        tenant_id: UUID,
        collection_id: UUID,
        document_id: UUID,
        document_version: int,
        event_id: UUID,
    ) -> int:
        """Insert one purge row per set citing the document (uq_purge_event keeps it idempotent).

        Matches ``(tenant_id, collection_id, document_id, document_version)`` against the
        chunks the set recorded. Returns the number of rows inserted.
        """
        ...


class QueryRefinementRepository(Protocol):
    """``query_refinements`` (writer: ``rag_app``)."""

    async def insert(self, tx: Tx, refinement: QueryRefinement) -> None:
        """Insert one refinement row."""
        ...


class AuditChainRepository(Protocol):
    """Append and read the audit chain."""

    async def append(self, tx: Tx, draft: AuditEntryDraft) -> AuditEntry | None:
        """Append in the caller's transaction; ``None`` where ``source_event_id`` already exists."""
        ...

    async def list_chain(self, tx: Tx, tenant_id: UUID, *, after_seq: int = 0) -> list[AuditEntry]:
        """Entries of one tenant in ``seq`` order."""
        ...


class VersionRepository(Protocol):
    """Writes on document versions that the generation slice owns."""

    async def mark_superseded_by_section_edit(
        self, tx: Tx, artefact_id: UUID, section_id: UUID
    ) -> list[UUID]:
        """AV-6: flag every document version whose section set includes ``section_id``.

        Returns the ids newly flagged (the ``superseded_version_ids`` of ``SectionCompleted``).
        """
        ...


class ArtefactPurgeRepository(Protocol):
    """The ART-9 delete."""

    async def list_artefacts_for_purge(
        self, tx: Tx, tenant_id: UUID, tender_id: UUID | None
    ) -> list[Artefact]:
        """The tender's artefacts, or every artefact of the tenant where ``tender_id`` is None."""
        ...

    async def delete_artefact_trees(
        self, tx: Tx, tenant_id: UUID, tender_id: UUID | None, artefact_ids: Sequence[UUID]
    ) -> None:
        """Delete ``artefact_ids`` and everything that hangs from them, in one transaction.

        Runs ``SET CONSTRAINTS ALL DEFERRED``. Also deletes the retrieval sets carrying the
        tender's ``tender_id`` (or every set of the tenant) with chunks and purge rows. The audit
        chain is never touched.
        """
        ...


class IdempotencyRace(Exception):  # noqa: N818
    """A concurrent admission took the ``Idempotency-Key`` first (``uq_run_idempotency``).

    Raised inside the admission transaction, so the shell insert rolls back with it; the caller
    re-reads the winner's run and answers by RUN-3.
    """


@dataclass(frozen=True, slots=True)
class CompletionRows:
    """Everything the completion transaction writes for one minted version (§7.5, GEN-10).

    ``section_version`` and ``version`` are built by the caller with the numbers
    ``next_version_numbers`` returned under the artefact lock.
    """

    artefact_version: ArtefactVersion | None
    """``None`` for a section regeneration, which mints no document version (REGEN-8)."""
    section_version: SectionVersion
    groundedness: GroundednessReport | None
    claims: tuple[ClaimVerification, ...]
    completeness: CompletenessCheck | None


class GenerationWriteRepository(Protocol):
    """Admission and completion SQL (``generation_runs``, shell, versions, verification rows)."""

    async def find_run_by_key(
        self, tx: Tx, tenant_id: UUID, principal_id: UUID, key: str
    ) -> GenerationRun | None:
        """The run holding ``(tenant, principal, key)``, or ``None`` (RUN-3)."""
        ...

    async def release_key(self, tx: Tx, run_id: UUID) -> None:
        """NULL the key of a run that failed or was cancelled, so a retry can reuse it."""
        ...

    async def insert_shell(
        self, tx: Tx, artefact: Artefact, run: GenerationRun, section: ArtefactSection | None
    ) -> None:
        """Insert artefact, run and its one section in the caller's transaction (RUN-1, LD-3).

        ``section`` is ``None`` for a long-form document, whose sections are the outline's rows
        (RUN-4). Raises ``IdempotencyRace`` where the run's key is already taken.
        """
        ...

    async def insert_run(self, tx: Tx, run: GenerationRun) -> None:
        """Insert a run against an existing artefact (A-5, A-6).

        Raises ``IdempotencyRace`` where ``uq_run_workflow`` (RUN-2) already holds the run's
        ``workflow_id``.
        """
        ...

    async def save_state(
        self, tx: Tx, artefact: Artefact, run: GenerationRun, section: ArtefactSection
    ) -> None:
        """Persist the status columns the three machines moved (and ``current_version_id``)."""
        ...

    async def save_run_and_section(
        self, tx: Tx, run: GenerationRun, section: ArtefactSection
    ) -> None:
        """``save_state`` without the artefact row.

        A section regeneration leaves the artefact at rest, and writing it back from a stale copy
        could undo a concurrent W-5 or A-5.
        """
        ...

    async def next_version_numbers(
        self, tx: Tx, artefact_id: UUID, section_id: UUID
    ) -> tuple[int, int]:
        """``(version_no, section_version_no)``: ``MAX + 1`` under the per-artefact lock (14.1)."""
        ...

    async def insert_completion(self, tx: Tx, rows: CompletionRows) -> None:
        """Insert the section version, the document version and its link, and the reports."""
        ...

    async def count_sections(self, tx: Tx, artefact_id: UUID) -> tuple[int, int]:
        """``(complete, total)`` section rows of the artefact, for ``SectionCompleted``."""
        ...


class RegenerationRepository(Protocol):
    """Reads and locks behind A-5, A-6 and A-7."""

    async def get_section(
        self, tx: Tx, artefact_id: UUID, ref: str
    ) -> tuple[ArtefactSection, SectionVersion | None] | None:
        """The section by ``tender_section_ref`` (or its id) with its current section version."""
        ...

    async def lock_section(self, tx: Tx, section_id: UUID) -> ArtefactSection | None:
        """Lock the section ``FOR UPDATE``: two regenerations of one section take turns.

        The second sees it ``generating`` and is refused.
        """
        ...

    async def document_holds_section_version(
        self, tx: Tx, artefact_version_id: UUID, section_version_id: UUID
    ) -> bool:
        """Whether the document version's roll-up includes the section version (REGEN-2)."""
        ...

    async def get_version(self, tx: Tx, artefact_version_id: UUID) -> ArtefactVersion | None:
        """A document version row, or ``None``."""
        ...


class FactRepository(Protocol):
    """``document_facts`` and ``fact_conflicts`` (§4.5.4, §4.5.5). Writer for these: ``rag_app``."""

    async def current_facts(self, tx: Tx, artefact_id: UUID) -> list[DocumentFact]:
        """Facts not superseded, by key."""
        ...

    async def insert_conflict(self, tx: Tx, conflict: FactConflict) -> None:
        """Surface a conflict (FC-1)."""
        ...

    async def get_conflict(
        self, tx: Tx, conflict_id: UUID, *, lock: bool = False
    ) -> FactConflict | None:
        """The conflict, ``FOR UPDATE`` when ``lock``; ``None`` also for another tenant's."""
        ...

    async def find_resolved(
        self, tx: Tx, section_id: UUID, fact_key: str, instruction_value: str
    ) -> FactConflict | None:
        """A conflict on this section, key and value that a reviewer already resolved."""
        ...

    async def save_resolution(self, tx: Tx, conflict: FactConflict) -> bool:
        """``WHERE resolution IS NULL``: ``False`` where another reviewer resolved it first."""
        ...

    async def supersede_fact(self, tx: Tx, artefact_id: UUID, fact_key: str, at: datetime) -> bool:
        """Mark the current fact superseded (DF-2); it is never overwritten."""
        ...

    async def insert_fact(self, tx: Tx, fact: DocumentFact) -> None:
        """Append the new current fact (after ``supersede_fact``: ``uq_fact_current``)."""
        ...

    async def flag_sections(
        self, tx: Tx, artefact_id: UUID, *, except_section_id: UUID, reason: str
    ) -> list[UUID]:
        """``flagged_for_review`` on the artefact's sections that have a version, except one."""
        ...


class InstructionRefinementRepository(Protocol):
    """``instruction_refinements`` (writer: ``rag_app``)."""

    async def insert(self, tx: Tx, row: InstructionRefinement) -> None:
        """One row per refiner call that ran (REF-1)."""
        ...

    async def record_resolution(self, tx: Tx, fact_conflict_id: UUID, resolution: str) -> None:
        """Stamp ``conflict_resolution`` on the refinement that surfaced the conflict (§4.6.2)."""
        ...


class Clock(Protocol):
    """Time source, injected so use cases are deterministic."""

    def now(self) -> datetime:
        """Current timezone-aware UTC time."""
        ...
