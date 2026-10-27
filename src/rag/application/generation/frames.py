"""The two-phase stream's frames (LLD §5.6) as the application hands them to the transport."""

from __future__ import annotations

from dataclasses import dataclass, field
from uuid import UUID

from rag.domain.generation.eligibility import EligibilityVerdict
from rag.domain.shared.enums import ClaimOutcome, CompletenessVerdict, DeliveryState, EvidenceTier


@dataclass(frozen=True, slots=True)
class DraftFrame:
    """Phase 1: a prose delta with provisional citation markers (never rewritten later)."""

    delta: str
    citation_refs: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class Citation:
    """One claim's resolved provenance (``Citation`` of §5.5). Corrections ride here only."""

    claim_ordinal: int
    outcome: ClaimOutcome
    cited_chunk_ids: tuple[str, ...]
    corrected_chunk_ids: tuple[str, ...]
    tier_decided: int


@dataclass(frozen=True, slots=True)
class GroundednessOut:
    """Claim counts and the tiers that ran."""

    supported: int = 0
    unsupported: int = 0
    miscited: int = 0
    gaps: int = 0
    tiers_run: tuple[int, ...] = ()


@dataclass(frozen=True, slots=True)
class CompletenessOut:
    """Completeness verdict with the missed items."""

    verdict: CompletenessVerdict = CompletenessVerdict.NOT_COMPUTED
    missed_items: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class FactConflictOut:
    """A surfaced fact conflict (§10.3): the choice A-7 takes. ``conflict_id`` is the row."""

    conflict_id: UUID
    fact_key: str
    established_value: str
    established_by_section_id: UUID
    instruction_value: str
    choices: tuple[str, ...] = ("update_fact", "keep_established", "dismiss")


@dataclass(frozen=True, slots=True)
class VerdictFrame:
    """Phase 2, the terminal frame (exactly one per response). Carries no generated text.

    ``artefact_version_id`` and ``version_no`` are ``None`` only on a refusal (no dispatch, no
    version): ``refusal`` then names why (insufficient evidence). ``replay`` marks a repeat
    answered from the stored artefact.
    """

    artefact_id: UUID
    generation_id: UUID
    retrieval_set_id: UUID | None
    evidence_tier: EvidenceTier
    eligibility: EligibilityVerdict
    delivery: DeliveryState
    artefact_version_id: UUID | None = None
    version_no: int | None = None
    citations: tuple[Citation, ...] = ()
    groundedness: GroundednessOut = field(default_factory=GroundednessOut)
    completeness: CompletenessOut = field(default_factory=CompletenessOut)
    degradation_markers: tuple[dict[str, str], ...] = ()
    refusal: dict[str, str] | None = None
    disclaimer_ref: str | None = None
    replay: bool = False
    section_version_id: UUID | None = None
    """A-6: the section version minted. ``artefact_version_id`` stays ``None`` (REGEN-8)."""
    prior_section_version_id: UUID | None = None
    """A-6: the version it replaces, so the two are comparable (§5.7.9)."""
    fact_conflict: FactConflictOut | None = None
    """A-6: the instruction contradicts an established fact; nothing was generated (D-35)."""


@dataclass(frozen=True, slots=True)
class ErrorFrame:
    """A failure after the first frame: ``INFERENCE_UNAVAILABLE`` or ``INTERNAL_ERROR`` only."""

    code: str
    detail: str


@dataclass(frozen=True, slots=True)
class Heartbeat:
    """An SSE comment while a stage holds the connection without producing a delta (§5.6)."""


type Frame = DraftFrame | VerdictFrame | ErrorFrame | Heartbeat
