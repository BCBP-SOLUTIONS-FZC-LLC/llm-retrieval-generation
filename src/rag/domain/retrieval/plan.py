"""The retrieval plan's value objects (§6.7) and the degradation marker (§5.5, RET-1)."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from rag.domain.packs.profile import ProfileSource
from rag.domain.retrieval.budget import ContextBudget


@dataclass(frozen=True, slots=True)
class DegradationMarker:
    """Appended by every stage that applied a fallback; ``degraded`` is derived from them."""

    stage: str
    reason: str
    applied_fallback: str

    def as_dict(self) -> dict[str, str]:
        """The §5.5 wire shape, also what ``degradation_markers`` stores."""
        return {
            "stage": self.stage,
            "reason": self.reason,
            "applied_fallback": self.applied_fallback,
        }


@dataclass(frozen=True, slots=True)
class StructuralFilters:
    """Optional predicates of the plan (§6.7). Entity filtering is not applied in v1."""

    document_roles: tuple[str, ...] = ()
    language: str | None = None


@dataclass(frozen=True, slots=True)
class RetrievalPlan:
    """Stage 3's output. Queries are in ``queries``; the scope is carried by the caller."""

    queries: tuple[str, ...]
    filters: StructuralFilters
    candidate_depth: int
    rerank_in: int
    rerank_out: int
    diversity_bound: int
    profile_source: ProfileSource
    values_provisional: bool
    budget: ContextBudget
    extra: dict[str, Any] = field(default_factory=dict)
    refiner_roles: tuple[str, ...] = ()
    """Roles the Refiner advised. They narrow only the refined and exact-term lists and the
    dense half, never the original query's lexical list (RET-2: a wrong guess cannot remove a
    path the raw query would have found). The pack's own ``filters.document_roles`` apply to all."""

    def applied_profile(self) -> dict[str, Any]:
        """The ``applied_profile`` block of R-1 and ``retrieval_sets.applied_profile``."""
        return {
            "source": self.profile_source.value,
            "values_provisional": self.values_provisional,
            "candidate_depth": self.candidate_depth,
            "rerank_in": self.rerank_in,
            "rerank_out": self.rerank_out,
        }
