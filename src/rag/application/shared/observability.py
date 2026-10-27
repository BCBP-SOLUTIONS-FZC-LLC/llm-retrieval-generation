"""§16.2 generation and verification instrumentation as a port.

Labels are bounded vocabularies (outcomes, tiers, delivery states, pack-declared section types,
Gateway model profiles). Never a tenant, user, request id or content (OBS-1, OBS-2).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from typing import Protocol


class GenerationObserver(Protocol):
    """Metrics of the generation, verification and streaming paths."""

    def span(self, name: str) -> AbstractAsyncContextManager[None]:
        """A trace span around one pipeline stage (§16.3). Attributes carry counts, never text."""
        ...

    def first_frame(self, seconds: float, suppressed: bool) -> None:
        """Time to the first frame of a stream (``suppressed``: phase 1 was withheld)."""
        ...

    def verified(self, kind: str, seconds: float) -> None:
        """Request start to verdict: ``answer``, ``draft`` or ``extraction``."""
        ...

    def verdict_frame(self, delivered: bool) -> None:
        """Whether the verdict frame reached the client (GEN-1)."""
        ...

    def delivery(self, state: str) -> None:
        """``delivered`` or ``disconnected`` on a written version."""
        ...

    def idempotent_replay(self) -> None:
        """A repeat served from a stored artefact (§5.7.2)."""
        ...

    def claim_outcome(self, outcome: str) -> None:
        """One verified claim's outcome (MD-16)."""
        ...

    def tier_decided(self, tier: int) -> None:
        """The ladder tier that settled a claim."""
        ...

    def groundedness(self, score: float | None) -> None:
        """A report's score, as a band (``None`` is not computed)."""
        ...

    def evidence_tier(self, tier: str) -> None:
        """The derived tier, ``NOT_COMPUTED`` included (AOB-2)."""
        ...

    def tier0_ratio(self, ratio: float) -> None:
        """Tier 0's claim-level disagreement, as a band."""
        ...

    def failure_policy(self, action: str) -> None:
        """``block`` or ``flag`` applied (§8.1)."""
        ...

    def completeness_missed(self, section_type: str | None, item: str) -> None:
        """A mandatory item the text did not address (§8.2)."""
        ...

    def gap_marker(self) -> None:
        """A gap claim, counted and never suppressed (VER-2)."""
        ...

    def tokens(self, task_class: str, model_profile: str, capability: str, count: int) -> None:
        """Tokens the Gateway reported (cost, §16.2)."""
        ...


class NullGenerationObserver:
    """The observer when metrics are off."""

    @asynccontextmanager
    async def span(self, name: str) -> AsyncIterator[None]:
        """No span."""
        yield

    def first_frame(self, seconds: float, suppressed: bool) -> None:
        """No-op."""

    def verified(self, kind: str, seconds: float) -> None:
        """No-op."""

    def verdict_frame(self, delivered: bool) -> None:
        """No-op."""

    def delivery(self, state: str) -> None:
        """No-op."""

    def idempotent_replay(self) -> None:
        """No-op."""

    def claim_outcome(self, outcome: str) -> None:
        """No-op."""

    def tier_decided(self, tier: int) -> None:
        """No-op."""

    def groundedness(self, score: float | None) -> None:
        """No-op."""

    def evidence_tier(self, tier: str) -> None:
        """No-op."""

    def tier0_ratio(self, ratio: float) -> None:
        """No-op."""

    def failure_policy(self, action: str) -> None:
        """No-op."""

    def completeness_missed(self, section_type: str | None, item: str) -> None:
        """No-op."""

    def gap_marker(self) -> None:
        """No-op."""

    def tokens(self, task_class: str, model_profile: str, capability: str, count: int) -> None:
        """No-op."""
