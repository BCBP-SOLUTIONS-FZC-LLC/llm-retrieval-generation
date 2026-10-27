"""Read-side composites returned by the generation repositories (A-1 to A-4, A-9)."""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from rag.domain.generation.artefact import Artefact, ArtefactVersion
from rag.domain.generation.section import ArtefactSection, SectionVersion
from rag.domain.generation.verification import (
    ClaimVerification,
    CompletenessCheck,
    GroundednessReport,
)
from rag.domain.shared.enums import EvidenceTier


@dataclass(frozen=True, slots=True)
class ArtefactView:
    """An artefact with its current version and the derived ``current_run_id`` (ART-3)."""

    artefact: Artefact
    current_version: ArtefactVersion | None
    current_run_id: UUID | None


@dataclass(frozen=True, slots=True)
class VersionSectionDetail:
    """One section of a document version with its verification records (per section, A-3)."""

    section: ArtefactSection
    section_version: SectionVersion
    groundedness: GroundednessReport | None
    claims: tuple[ClaimVerification, ...]
    completeness: CompletenessCheck | None


@dataclass(frozen=True, slots=True)
class VersionDetail:
    """A version in full (A-3)."""

    version: ArtefactVersion
    sections: tuple[VersionSectionDetail, ...]


@dataclass(frozen=True, slots=True)
class SectionStateView:
    """A section's state with its current version's tier (A-4); ``None`` while it has none."""

    section: ArtefactSection
    evidence_tier: EvidenceTier | None


@dataclass(frozen=True, slots=True)
class SectionVersionRecord:
    """A section version with its section, for A-9."""

    section: ArtefactSection
    section_version: SectionVersion
