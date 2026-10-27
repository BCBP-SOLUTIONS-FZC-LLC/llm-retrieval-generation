"""Score and tier derivation, and the combined verdict (LLD §8.3, §8.4; VER-4, VER-5, GR-4).

Arithmetic over recorded claim counts, so a roll-up is reproducible rather than a second
judgement. Thresholds are configuration defaults, not calibrated values (D-23, D-26).
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal
from enum import StrEnum
from fractions import Fraction

from rag.domain.shared.enums import CompletenessVerdict, EvidenceTier


@dataclass(frozen=True, slots=True)
class TierThresholds:
    """§17 ``GROUNDEDNESS_PARTIAL_THRESHOLD`` and ``GROUNDEDNESS_SUPPORTED_THRESHOLD``."""

    partial: float = 0.70
    supported: float = 0.95


@dataclass(frozen=True, slots=True)
class Scored:
    """The score (``None`` is ``NOT_COMPUTED``, never zero, VER-5) and the tier."""

    score: Decimal | None
    tier: EvidenceTier


def derive_score_and_tier(
    *,
    supported: int,
    unsupported: int,
    miscited: int,
    gaps: int,
    compliance: bool,
    thresholds: TierThresholds = TierThresholds(),  # noqa: B008 - frozen
    verification_ran: bool = True,
) -> Scored:
    """``score = (supported + miscited) / (claims_total - gaps)``; the tier follows.

    Miscited claims count as supported once corrected (§8.4). Gaps leave the denominator. The
    compliance gate (GR-4) makes any unsupported claim ``INSUFFICIENT``; elsewhere an unsupported
    claim caps the tier at ``PARTIAL`` because ``SUPPORTED`` demands none. Comparisons use exact
    fractions so the tier recomputes identically from the stored counts (VER-4).
    """
    denominator = supported + unsupported + miscited
    if not verification_ran or denominator == 0:
        return Scored(None, EvidenceTier.NOT_COMPUTED)
    exact = Fraction(supported + miscited, denominator)
    rounded = (Decimal(supported + miscited) / Decimal(denominator)).quantize(
        Decimal("0.001"), rounding=ROUND_HALF_UP
    )
    if (compliance and unsupported > 0) or exact < Fraction(str(thresholds.partial)):
        tier = EvidenceTier.INSUFFICIENT
    elif exact >= Fraction(str(thresholds.supported)) and unsupported == 0:
        tier = EvidenceTier.SUPPORTED
    else:
        tier = EvidenceTier.PARTIAL
    return Scored(rounded, tier)


class Disposition(StrEnum):
    """What the combined verdict asks the caller to do (§8.3)."""

    SURFACE = "surface"
    SURFACE_FLAGGED = "surface_flagged"
    RETRY = "retry"


def combined_verdict(tier: EvidenceTier, completeness: CompletenessVerdict) -> Disposition:
    """Both signals must exist before the failure policy runs (§8.3).

    Grounded and Complete surfaces; Partial on either surfaces flagged; below the groundedness
    gate or ``Insufficient`` completeness enters the retry path. ``NOT_COMPUTED`` is never a
    pass (AOB-2): it surfaces flagged. A section with no checklist has no completeness signal
    and is judged on groundedness alone.
    """
    if tier is EvidenceTier.INSUFFICIENT or completeness is CompletenessVerdict.INSUFFICIENT:
        return Disposition.RETRY
    if tier in (EvidenceTier.PARTIAL, EvidenceTier.NOT_COMPUTED) or (
        completeness is CompletenessVerdict.PARTIAL
    ):
        return Disposition.SURFACE_FLAGGED
    return Disposition.SURFACE
