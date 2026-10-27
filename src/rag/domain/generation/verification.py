"""Verification records (LLD 4.5.1-4.5.3). Written by activities (S-5); read here."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from uuid import UUID

from rag.domain.generation.exceptions import InvariantViolation
from rag.domain.shared.enums import ClaimOutcome, CompletenessVerdict, EvidenceTier


@dataclass(frozen=True, slots=True)
class GroundednessReport:
    """One per section version. Claims are scored against the retrieval set only (GR-1)."""

    id: UUID
    section_version_id: UUID
    tenant_id: UUID
    tiers_run: tuple[int, ...]
    claims_total: int
    supported: int
    unsupported: int
    miscited: int
    gaps: int
    evidence_tier: EvidenceTier
    score: Decimal | None = None
    judge_model_profile_id: str | None = None
    created_at: datetime | None = None

    def __post_init__(self) -> None:
        if self.claims_total != self.supported + self.unsupported + self.miscited + self.gaps:
            raise InvariantViolation("GR", "claims_total must equal the four outcome counts")
        if self.score is None and self.evidence_tier is not EvidenceTier.NOT_COMPUTED:
            raise InvariantViolation("VER-5", "a NULL score is NOT_COMPUTED, never zero")


@dataclass(frozen=True, slots=True)
class ClaimVerification:
    """One verified claim; ``claim_ordinal`` is stable within one report only (CV-1)."""

    report_id: UUID
    claim_ordinal: int
    tenant_id: UUID
    outcome: ClaimOutcome
    cited_chunk_ids: tuple[str, ...]
    tier_decided: int
    corrected_chunk_ids: tuple[str, ...] | None = None

    def __post_init__(self) -> None:
        if self.corrected_chunk_ids and self.outcome is not ClaimOutcome.MISCITED:
            raise InvariantViolation("CV-3", "corrected_chunk_ids only where outcome is miscited")


@dataclass(frozen=True, slots=True)
class CompletenessCheck:
    """One per section version, against the pack's mandatory-clause list (CC-1)."""

    id: UUID
    section_version_id: UUID
    tenant_id: UUID
    verdict: CompletenessVerdict
    items_total: int
    items_addressed: int
    missed_items: tuple[str, ...] = field(default_factory=tuple)
    created_at: datetime | None = None

    def __post_init__(self) -> None:
        if self.items_addressed > self.items_total:
            raise InvariantViolation("CC", "items_addressed cannot exceed items_total")
