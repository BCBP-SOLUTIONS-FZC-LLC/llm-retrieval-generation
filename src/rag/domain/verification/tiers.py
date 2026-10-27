"""The tiered ladder's decision logic (LLD §8.1): pure, no model call, no I/O.

Numbering follows the LLD: tier 0 self-consistency, tier 1 citation-span overlap, tier 2
entailment, tier 3 judge. The model calls sit behind ports (``ports.py``); this module decides
what each result means and where a claim routes next. The three routing figures are unvalidated
design defaults carried in configuration (D-23, D-26).
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum

from rag.domain.shared.enums import ClaimOutcome
from rag.domain.verification.claims import Claim

_WORD = re.compile(r"\w+", re.UNICODE)
_STOP = frozenset(
    "a an and are as at be by for from has have in is it its of on or that the this to was were "
    "with which shall will must should".split()
)


@dataclass(frozen=True, slots=True)
class LadderThresholds:
    """§8.1's routing figures. Defaults are the LLD's, not calibrated."""

    span_overlap: float = 0.60
    entail_supported: float = 0.70
    entail_unsupported: float = 0.40
    tier0_disagreement: float = 0.20
    claim_agreement: float = 0.50
    """Token overlap above which two claims from the two samples count as the same claim."""


@dataclass(frozen=True, slots=True)
class EvidenceChunk:
    """A retrieved chunk as verification sees it: the id and the full text (GR-1)."""

    chunk_id: str
    text: str


class Tier1(StrEnum):
    """What the in-process span match decided."""

    SUPPORTED = "supported"
    ROUTE_TO_TIER2 = "route_to_tier2"


class Tier2(StrEnum):
    """What an entailment score means (§8.1)."""

    SUPPORTED = "supported"
    UNSUPPORTED = "unsupported"
    UNCERTAIN = "uncertain"


def tokens(text: str) -> frozenset[str]:
    """Case-folded word set without stop words: the unit of overlap."""
    return frozenset(w for w in (m.casefold() for m in _WORD.findall(text)) if w not in _STOP)


def overlap(claim_text: str, passage: str) -> float:
    """Share of the claim's tokens that occur in ``passage`` (claim-side recall, 0 to 1)."""
    claim = tokens(claim_text)
    return len(claim & tokens(passage)) / len(claim) if claim else 0.0


@dataclass(frozen=True, slots=True)
class Tier1Result:
    """Tier 1's verdict plus the best other chunk, kept as the miscitation candidate."""

    decision: Tier1
    best_cited: float
    correction: str | None = None


def tier1(
    claim_text: str,
    cited: Sequence[str],
    chunks: Mapping[str, str],
    th: LadderThresholds,
) -> Tier1Result:
    """Citation-span matching: supported at or above the overlap threshold with a cited chunk.

    Below it the claim routes to tier 2 (§8.1). Independently, the best chunk the claim did not
    cite at or above the threshold is remembered as the correction a miscitation would carry.
    """
    best = max((overlap(claim_text, chunks[c]) for c in cited if c in chunks), default=0.0)
    others = [(overlap(claim_text, text), cid) for cid, text in chunks.items() if cid not in cited]
    top = max(others, default=(0.0, ""))
    correction = top[1] if top[0] >= th.span_overlap else None
    decision = Tier1.SUPPORTED if best >= th.span_overlap else Tier1.ROUTE_TO_TIER2
    return Tier1Result(decision, best, correction)


def tier2(score: float, th: LadderThresholds) -> Tier2:
    """Entailment: at or above 0.70 supported, below 0.40 unsupported, between them tier 3."""
    if score >= th.entail_supported:
        return Tier2.SUPPORTED
    if score < th.entail_unsupported:
        return Tier2.UNSUPPORTED
    return Tier2.UNCERTAIN


def settle_negative(correction: str | None) -> tuple[ClaimOutcome, tuple[str, ...]]:
    """A claim its cited chunks do not support: miscited where another chunk does (§8.1)."""
    if correction is not None:
        return ClaimOutcome.MISCITED, (correction,)
    return ClaimOutcome.UNSUPPORTED, ()


def tier0_disagreement(
    first: Sequence[Claim], second: Sequence[Claim], th: LadderThresholds
) -> float:
    """Claim-level diff of two samples: the share of claims, both ways, with no counterpart.

    A claim agrees when some claim of the other sample overlaps it at ``claim_agreement``.
    Gap claims are ignored: declining to assert is not a disagreement. Two empty samples agree.
    """
    a = [c.text for c in first if not c.gap]
    b = [c.text for c in second if not c.gap]
    if not a and not b:
        return 0.0
    miss_a = sum(1 for x in a if max((overlap(x, y) for y in b), default=0.0) < th.claim_agreement)
    miss_b = sum(1 for y in b if max((overlap(y, x) for x in a), default=0.0) < th.claim_agreement)
    return (miss_a + miss_b) / (len(a) + len(b))


def top_candidates(
    claim_text: str, cited: Sequence[str], chunks: Mapping[str, str], limit: int = 3
) -> list[str]:
    """The ``limit`` chunks the claim did not cite that overlap it most (judge candidates)."""
    ranked = sorted(
        ((overlap(claim_text, text), cid) for cid, text in chunks.items() if cid not in cited),
        key=lambda pair: (-pair[0], pair[1]),
    )
    return [cid for score, cid in ranked[:limit] if score > 0.0]


_NON_DIGITS = re.compile(r"[\s,._'’]")


def _flat(text: str) -> str:
    return _NON_DIGITS.sub("", text.casefold())


def value_in_text(value: object, text: str) -> bool:
    """Whether an extracted ``value`` appears in ``text``: the tier-1 test of an extraction field.

    Numbers match regardless of thousands separators and spacing; strings match case-folded and
    whitespace-collapsed. A value the passage does not contain is not supported by it.
    """
    if isinstance(value, bool):
        return True  # a boolean has no surface form in the text; the model's chunk ids carry it
    flat_text = _flat(text)
    if isinstance(value, int | float):
        shown = f"{value:g}" if isinstance(value, float) else str(value)
        return _flat(shown) in flat_text or _flat(str(value)) in flat_text
    needle = _flat(str(value))
    return bool(needle) and needle in flat_text
