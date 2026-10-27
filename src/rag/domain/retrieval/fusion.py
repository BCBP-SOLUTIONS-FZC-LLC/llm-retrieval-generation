"""Reciprocal rank fusion with a collection rank-list prior (§6.7, LD-24) and text dedup."""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from dataclasses import dataclass, replace
from decimal import ROUND_HALF_UP, Decimal

from rag.domain.retrieval.candidate import Candidate


def rank_offset(weight: Decimal, k: int) -> int:
    """``round(k * (1 - w) / w)``, half up, in exact decimal arithmetic.

    Weights are ``numeric(3,2)``; a float ``round`` is banker's rounding and lands either side
    of ``.5`` (``k=60, w=0.96`` is exactly 2.5), so a silent change would move every score.
    """
    if not Decimal("0") < weight <= Decimal("1"):
        raise ValueError("collection weight must be in (0, 1]")
    return int((Decimal(k) * (Decimal(1) - weight) / weight).quantize(Decimal(1), ROUND_HALF_UP))


@dataclass(frozen=True, slots=True)
class RankedList:
    """One ranked input list. ``rank_shift`` moves the whole list (negative raises its prior)."""

    name: str
    items: Sequence[Candidate]
    rank_shift: int = 0


def text_key(text: str) -> str:
    """Hash of a chunk's own text, the dedup key (§6.7: the payload carries no hash field)."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def rrf_fuse(lists: Sequence[RankedList], k: int) -> list[Candidate]:
    """Fuse ``lists`` best-first.

    Contribution: ``1 / (k + rank + offset(weight) + rank_shift)`` with 1-based ranks, the
    denominator floored at ``k`` so a boosted list never divides by less than a rank-1 hit.
    Identical chunk text is collapsed across the whole candidate set, keeping the
    highest-ranked copy (ties: the smaller ``chunk_id``). ``source_half`` becomes ``both`` where
    a chunk came from a dense and a lexical list.
    """
    score: dict[str, float] = {}
    first: dict[str, Candidate] = {}
    halves: dict[str, set[str]] = {}
    for ranked in lists:
        half = "dense" if ranked.name.startswith("dense") else "lexical"
        for position, cand in enumerate(ranked.items, start=1):
            denominator = max(k + position + rank_offset(cand.weight, k) + ranked.rank_shift, k)
            score[cand.chunk_id] = score.get(cand.chunk_id, 0.0) + 1.0 / denominator
            first.setdefault(cand.chunk_id, cand)
            halves.setdefault(cand.chunk_id, set()).add(half)
    ordered = sorted(score, key=lambda cid: (-score[cid], cid))
    seen: set[str] = set()
    fused: list[Candidate] = []
    for chunk_id in ordered:
        cand = first[chunk_id]
        key = text_key(cand.text)
        if key in seen:
            continue
        seen.add(key)
        half = "both" if len(halves[chunk_id]) > 1 else next(iter(halves[chunk_id]))
        fused.append(replace(cand, fused_score=score[chunk_id], source_half=half))
    return fused
