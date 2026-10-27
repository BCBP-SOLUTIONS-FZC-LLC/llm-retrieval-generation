"""Claim extraction from generated prose (LLD §8.1): sentences, citation markers, gap markers.

Claims are what the ladder verifies. The generator is told to cite each factual sentence with
``[c-NN]`` and to write ``[GAP: reason]`` where the evidence runs out (§7.2), so the parse is
mechanical: no model call.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

_CITATION = re.compile(r"\[(c-\d+(?:\s*,\s*c-\d+)*)\]")
_GAP = re.compile(r"\[GAP\b[^\]]*\]", re.IGNORECASE)
_SENTENCE = re.compile(r"(?<=[.!?])\s+|\n+")
_WORD = re.compile(r"\w+", re.UNICODE)
MIN_CLAIM_WORDS = 3
"""Fragments shorter than this (headings, list stubs) assert nothing and are not claims."""


@dataclass(frozen=True, slots=True)
class Claim:
    """One asserted sentence: its text without markers, the refs it cited, whether it is a gap."""

    ordinal: int
    text: str
    cited: tuple[str, ...] = ()
    gap: bool = False


def parse_claims(text: str) -> list[Claim]:
    """Split ``text`` into claims, ordinals from 1 in reading order.

    A sentence holding a gap marker is a ``gap`` claim whatever else it says. Refs keep the
    model's spelling (``c-07``); resolving them against the retrieval set is the verifier's job.
    """
    claims: list[Claim] = []
    for raw in _SENTENCE.split(text):
        sentence = raw.strip()
        if not sentence:
            continue
        cited = tuple(
            dict.fromkeys(
                ref.strip().lower() for m in _CITATION.finditer(sentence) for ref in m[1].split(",")
            )
        )
        gap = _GAP.search(sentence) is not None
        plain = " ".join(_GAP.sub(" ", _CITATION.sub(" ", sentence)).split())
        if not gap and len(_WORD.findall(plain)) < MIN_CLAIM_WORDS:
            continue
        claims.append(Claim(len(claims) + 1, plain, cited, gap))
    return claims
