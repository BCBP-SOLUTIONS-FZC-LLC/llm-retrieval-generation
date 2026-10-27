"""Section context budget and trim (§6.8): allocations as fractions of the resolved window."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from rag.domain.retrieval.candidate import Candidate
from rag.domain.shared.errors import DomainError, ErrorCode

SKELETON_MAX_FRACTION = 0.15


@dataclass(frozen=True, slots=True)
class ContextBudget:
    """Token allocation resolved at plan time (MD-26)."""

    window_tokens: int
    output_reserve_tokens: int
    skeleton_tokens: int
    user_input_tokens: int
    evidence_tokens: int

    def as_dict(self) -> dict[str, int]:
        """For ``retrieval_sets.budget_allocation``."""
        return {
            "window_tokens": self.window_tokens,
            "output_reserve_tokens": self.output_reserve_tokens,
            "skeleton_tokens": self.skeleton_tokens,
            "user_input_tokens": self.user_input_tokens,
            "evidence_tokens": self.evidence_tokens,
        }


def plan_budget(
    window_tokens: int,
    *,
    output_fraction: float,
    user_input_max_fraction: float,
    skeleton_tokens: int,
    user_input_tokens: int,
) -> ContextBudget:
    """Reserve output first, then system + user input; evidence is the residual (RET-6).

    ``413 USER_INPUT_OVER_BUDGET`` where the user input exceeds its share (never trimmed);
    ``422 PACK_INVALID`` where the skeleton exceeds 15% of the window (LD-63).
    """
    if skeleton_tokens > SKELETON_MAX_FRACTION * window_tokens:
        raise DomainError(ErrorCode.PACK_INVALID, "The prompt skeleton exceeds 15% of the window.")
    if user_input_tokens > user_input_max_fraction * window_tokens:
        raise DomainError(ErrorCode.USER_INPUT_OVER_BUDGET, "The input exceeds its token budget.")
    output = int(window_tokens * output_fraction)
    residual = window_tokens - output - skeleton_tokens - user_input_tokens
    return ContextBudget(
        window_tokens, output, skeleton_tokens, user_input_tokens, max(residual, 0)
    )


def candidate_depth(
    profile_depth: int, budget: ContextBudget, mean_chunk_tokens: int, overfetch: float
) -> int:
    """``min(profile_depth, residual / mean_chunk_tokens * overfetch)``, at least 1 (§6.7)."""
    fits = int(budget.evidence_tokens / max(mean_chunk_tokens, 1) * overfetch)
    return max(1, min(profile_depth, fits))


@dataclass(frozen=True, slots=True)
class Protection:
    """Chunks a trim must not drop: the last one of a required role, a mandatory clause."""

    required_roles: frozenset[str] = frozenset()
    mandatory_clauses: frozenset[str] = frozenset()

    def protects(self, chunk: Candidate, others: Sequence[Candidate]) -> bool:
        """Whether dropping ``chunk`` (the rest staying in ``others``) loses a requirement."""
        if chunk.clause_ref is not None and chunk.clause_ref in self.mandatory_clauses:
            return True
        return chunk.document_role in self.required_roles and not any(
            o.document_role == chunk.document_role for o in others
        )


@dataclass(frozen=True, slots=True)
class TrimResult:
    """Kept chunks in rank order, and why the trim refused where it did (never silent)."""

    kept: tuple[Candidate, ...]
    dropped: tuple[Candidate, ...]
    refused: str | None = None


def trim_to_budget(
    chunks: Sequence[Candidate],
    tokens: Sequence[int],
    evidence_tokens: int,
    *,
    diversity_bound: int,
    protection: Protection = Protection(),  # noqa: B008 - frozen value object
) -> TrimResult:
    """Diversity bound, then drop from the bottom of the reranked order until it fits (§6.8).

    A trim that would remove a protected chunk refuses: ``refused`` is set, ``kept`` is the
    untrimmed input, and the caller reports ``insufficient`` rather than trim silently.
    """
    per_doc: dict[object, int] = {}
    kept: list[tuple[Candidate, int]] = []
    dropped: list[Candidate] = []
    for chunk, cost in zip(chunks, tokens, strict=True):  # lowest-ranked of a document go first
        per_doc[chunk.document_id] = per_doc.get(chunk.document_id, 0) + 1
        if per_doc[chunk.document_id] > diversity_bound:
            dropped.append(chunk)
        else:
            kept.append((chunk, cost))
    total = sum(cost for _, cost in kept)
    while kept and total > evidence_tokens:
        victim, cost = kept.pop()
        dropped.append(victim)
        total -= cost
    survivors = [c for c, _ in kept]
    if any(protection.protects(d, survivors) for d in dropped):
        return TrimResult(tuple(chunks), (), "trim_would_drop_required_evidence")
    return TrimResult(tuple(survivors), tuple(dropped))
