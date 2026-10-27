"""Document evidence-tier roll-up (LLD 8.6, MD-30, AOB-2, AOB-3)."""

from __future__ import annotations

from collections.abc import Iterable

from rag.domain.shared.enums import EvidenceTier


def roll_up_evidence_tier(section_tiers: Iterable[EvidenceTier]) -> EvidenceTier:
    """The document tier is the lowest section tier present.

    ``EvidenceTier`` orders ``NOT_COMPUTED`` lowest, so ``min()`` makes an uncomputed section
    propagate and never average away (AOB-2). A document with no sections has nothing computed,
    so it is ``NOT_COMPUTED``: an empty set must never read as passing.
    """
    return min(section_tiers, default=EvidenceTier.NOT_COMPUTED)
