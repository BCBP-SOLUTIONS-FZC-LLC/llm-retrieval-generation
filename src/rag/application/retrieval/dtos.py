"""Commands and results of the retrieval module."""

from __future__ import annotations

from dataclasses import dataclass, field
from uuid import UUID

from rag.application.retrieval.scope import ScopeSpec
from rag.domain.generation.refinement import QueryRefinement
from rag.domain.retrieval.candidate import Candidate
from rag.domain.retrieval.plan import DegradationMarker, RetrievalPlan
from rag.domain.retrieval.scope import ResolvedScope
from rag.domain.retrieval.sufficiency import GapReport
from rag.domain.shared.enums import GrantLevel, RetrievalVerdict


@dataclass(frozen=True, slots=True)
class PipelineRequest:
    """Input of the pipeline, shared by R-1 and (later) the generation routes.

    ``required_level`` is the route's level (§5.3). ``pinned_pack_version`` re-reads the pack
    version a run pinned (``ASYNC-3``). ``enforce_required_inputs`` raises
    ``422 REQUIRED_INPUT_MISSING`` on a blocking gap (R-3/A-6 behaviour, §6.9).
    """

    query: str
    scope: ScopeSpec = field(default_factory=ScopeSpec)
    section_type: str | None = None
    limit: int | None = None
    required_level: GrantLevel = GrantLevel.SEARCH
    domain_profile_ref: str | None = None
    language: str | None = None
    pinned_pack_version: str | None = None
    enforce_required_inputs: bool = False


@dataclass(frozen=True, slots=True)
class RetrievalOutcome:
    """A retrieval reduced to its verdict (§6.1), ready to be recorded.

    ``first_attempt`` is set on the one widened retry (D-16) and holds the insufficient first
    outcome, which is recorded too (RS-6). ``refinement_persisted`` is true where the
    refinement row already exists (a cache hit reuses it).
    """

    scope: ResolvedScope
    verdict: RetrievalVerdict
    verdict_reason: str | None
    chunks: tuple[Candidate, ...]
    markers: tuple[DegradationMarker, ...]
    plan: RetrievalPlan
    reranker_ran: bool
    domain_profile_version: str
    language_pack_version: str
    refinement: QueryRefinement | None = None
    refinement_persisted: bool = False
    widened_retry: bool = False
    first_attempt: RetrievalOutcome | None = None
    cache_hit: bool = False

    @property
    def degraded(self) -> bool:
        """Derived, never set by hand (§6.1): any marker means reduced quality."""
        return len(self.markers) > 0


@dataclass(frozen=True, slots=True)
class SearchCommand:
    """R-1 after validation."""

    request: PipelineRequest


@dataclass(frozen=True, slots=True)
class SearchResult:
    """R-1's result: the recorded set's id and the outcome."""

    retrieval_set_id: UUID
    outcome: RetrievalOutcome
    widened_retry_of: UUID | None = None


@dataclass(frozen=True, slots=True)
class RequiredInputCommand:
    """R-7 after validation."""

    tender_id: UUID | None
    scope: ScopeSpec
    capability: str | None
    section_types: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class RequiredInputResult:
    """R-7's gap report per section type, and the profile version it ran under."""

    per_section: dict[str, GapReport]
    profile_version: str


@dataclass(frozen=True, slots=True)
class ClassificationCommand:
    """R-6 after validation."""

    content: str
    filename: str | None = None
    domain_profile_ref: str | None = None


@dataclass(frozen=True, slots=True)
class RoleAlternative:
    """One alternative role and its confidence."""

    role: str
    confidence: float


@dataclass(frozen=True, slots=True)
class ClassificationResult:
    """R-6's proposal; stored nowhere (MD-21, GEN-9)."""

    proposed_role: str
    confidence: float
    alternatives: tuple[RoleAlternative, ...]
    taxonomy_version: str
