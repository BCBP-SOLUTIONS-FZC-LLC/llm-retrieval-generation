"""Ports of the ladder's model calls. Verification receives data and calls out through these."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol

from rag.domain.verification.tiers import EvidenceChunk


class VerificationUnavailable(Exception):  # noqa: N818
    """A model call of the ladder cannot run (Gateway down, refused, malformed answer).

    Not a verdict: a compliance section must fail on it (GR-3, GEN-7); any other section is
    written with tier ``NOT_COMPUTED`` (§14.4).
    """


@dataclass(frozen=True, slots=True)
class Entailment:
    """Tier 2's answer: a score in 0 to 1 and the Model Profile that gave it."""

    score: float
    model: str = ""


@dataclass(frozen=True, slots=True)
class JudgeVerdict:
    """Tier 3's answer for one claim: supported by the passages, or by which other chunks."""

    supported: bool
    corrected_chunk_ids: tuple[str, ...] = ()
    model: str = ""


class Entailer(Protocol):
    """Tier 2: one small model call scoring whether a passage entails a claim."""

    async def score(self, claim: str, passage: EvidenceChunk) -> Entailment:
        """Whether ``passage`` entails ``claim``. Raises ``VerificationUnavailable``."""
        ...


class Judge(Protocol):
    """Tier 3: LLM-as-judge on a different Model Profile than generation (VER-3)."""

    async def judge(
        self, claim: str, cited: Sequence[EvidenceChunk], candidates: Sequence[EvidenceChunk]
    ) -> JudgeVerdict:
        """Whether ``cited`` supports ``claim``; else which of ``candidates`` does.

        Raises ``VerificationUnavailable``.
        """
        ...


class Resampler(Protocol):
    """Tier 0: the second generation (one extra draft call) whose claims are diffed."""

    async def sample(self) -> str:
        """A second independent generation of the prompt. Raises ``VerificationUnavailable``."""
        ...
