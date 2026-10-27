"""Required-input checks and the three-valued sufficiency verdict (§6.9, MD-14)."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from uuid import UUID

from rag.domain.shared.enums import RetrievalVerdict


@dataclass(frozen=True, slots=True)
class SufficiencyThreshold:
    """Score-based judge parameters (D-15): top reranked score and chunks above a floor."""

    min_top_score: float
    min_chunks: int
    chunk_score_floor: float


@dataclass(frozen=True, slots=True)
class RequiredInputRule:
    """What a section type needs in scope (HLD §7.9, §22.9)."""

    document_roles: tuple[str, ...] = ()
    resolvable_entities: tuple[str, ...] = ()
    non_empty_collections: bool = False


@dataclass(frozen=True, slots=True)
class GapReport:
    """§5.5 ``GapReport``. A verdict wherever the caller chose to proceed."""

    missing_roles: tuple[str, ...] = ()
    unresolved_entities: tuple[str, ...] = ()
    empty_collections: tuple[UUID, ...] = ()
    blocking: bool = False


def evaluate_required_inputs(
    rule: RequiredInputRule,
    *,
    role_counts: Mapping[str, int],
    entity_counts: Mapping[str, int],
    empty_collections: Sequence[UUID],
) -> GapReport:
    """Reduce counts to a gap report. Deterministic, no model call (LD-100).

    A role is missing at count zero (``unknown`` never satisfies a role rule: it is a
    different filter value). An entity type with a zero count is unresolved. Empty collections
    (SCOPE-12) are always named; they block only where the rule requires them non-empty.
    """
    missing = tuple(r for r in rule.document_roles if role_counts.get(r, 0) <= 0)
    unresolved = tuple(e for e in rule.resolvable_entities if entity_counts.get(e, 0) <= 0)
    empties = tuple(sorted(empty_collections, key=str))
    blocking = bool(missing or unresolved or (rule.non_empty_collections and empties))
    return GapReport(missing, unresolved, empties, blocking)


@dataclass(frozen=True, slots=True)
class Judgement:
    """Verdict plus the reason a non-sufficient one always carries (RS-5)."""

    verdict: RetrievalVerdict
    reason: str | None = None


def judge(
    scores: Sequence[float | None], threshold: SufficiencyThreshold, *, reranked: bool
) -> Judgement:
    """Compare the assembled set against the profile threshold (HLD §7.10).

    ``scores`` are the chunks' rerank scores in rank order. Where the reranker did not run,
    fused RRF scores are not comparable with a threshold set for reranker output, so only the
    chunk count is judged (a deliberate simplification, recorded in the plan).
    """
    if not scores:
        return Judgement(RetrievalVerdict.INSUFFICIENT, "no_evidence_retrieved")
    if not reranked:
        if len(scores) < threshold.min_chunks:
            return Judgement(RetrievalVerdict.INSUFFICIENT, "too_few_chunks")
        return Judgement(RetrievalVerdict.SUFFICIENT)
    values = [s for s in scores if s is not None]
    if not values or max(values) < threshold.min_top_score:
        return Judgement(RetrievalVerdict.INSUFFICIENT, "top_score_below_threshold")
    above = sum(1 for s in values if s >= threshold.chunk_score_floor)
    if above < threshold.min_chunks:
        return Judgement(RetrievalVerdict.INSUFFICIENT, "too_few_chunks_above_floor")
    return Judgement(RetrievalVerdict.SUFFICIENT)
