"""Read composite of W-2: the run, the artefact's sections and the surfaced state (§5.7.13)."""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from rag.domain.generation.artefact import Artefact
from rag.domain.generation.facts import DocumentFact
from rag.domain.generation.run import GenerationRun
from rag.domain.generation.section import ArtefactSection
from rag.domain.shared.enums import EvidenceTier


@dataclass(frozen=True, slots=True)
class SectionProgress:
    """A section row with its current version's tier and retrieval set (``None`` before one)."""

    section: ArtefactSection
    evidence_tier: EvidenceTier | None = None
    retrieval_set_id: UUID | None = None


@dataclass(frozen=True, slots=True)
class RunStateView:
    """Everything W-2 serves, read from this service's tables only (ASYNC-5)."""

    run: GenerationRun
    artefact: Artefact
    sections: tuple[SectionProgress, ...]
    facts: tuple[DocumentFact, ...]
    scope_narrowed: bool
