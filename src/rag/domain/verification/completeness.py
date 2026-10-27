"""Completeness against the pack's mandatory-clause list (LLD §8.2, CC-1): no model call."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from rag.domain.shared.enums import CompletenessVerdict
from rag.domain.verification.tiers import tokens


@dataclass(frozen=True, slots=True)
class Completeness:
    """Verdict with the figures ``completeness_checks`` stores."""

    verdict: CompletenessVerdict
    items_total: int
    items_addressed: int
    missed_items: tuple[str, ...] = ()


def check_completeness(
    text: str, mandatory: Iterable[str], *, partial_threshold: float = 0.70
) -> Completeness:
    """An item is addressed when its phrase, or every significant token of it, is in ``text``.

    ``Complete`` when all are addressed, ``Partial`` at or above ``partial_threshold``,
    ``Insufficient`` below it. An empty checklist reports ``NOT_COMPUTED`` rather than
    ``Complete``: a thin or absent checklist must not silently read as complete (§8.2).
    """
    items = sorted(set(mandatory))
    if not items:
        return Completeness(CompletenessVerdict.NOT_COMPUTED, 0, 0)
    haystack, have = text.casefold(), tokens(text)
    missed = tuple(
        item
        for item in items
        if item.casefold() not in haystack and not (tokens(item) and tokens(item) <= have)
    )
    addressed = len(items) - len(missed)
    ratio = addressed / len(items)
    if not missed:
        verdict = CompletenessVerdict.COMPLETE
    elif ratio >= partial_threshold:
        verdict = CompletenessVerdict.PARTIAL
    else:
        verdict = CompletenessVerdict.INSUFFICIENT
    return Completeness(verdict, len(items), addressed, missed)
