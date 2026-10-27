"""Small text rules of retrieval: query normalisation (cache keys) and excerpt snapping (§7.7)."""

from __future__ import annotations

import re

_SPACE = re.compile(r"\s+")
_SENTENCE_END = re.compile(r"[.!?][\"')\]]?(?=\s)")


def normalise_query(query: str) -> str:
    """Case-folded, whitespace-collapsed: the form cache keys and refiner cache use (§11.1)."""
    return _SPACE.sub(" ", query).strip().casefold()


def snap_excerpt(text: str, limit: int = 240) -> str:
    """At most ``limit`` characters of ``text``, ending at a sentence end where one is reasonable.

    ``text`` is the chunk's own text and is never stored whole (§4.10). A cut falls back to the
    last word boundary so a word is never split.
    """
    flat = _SPACE.sub(" ", text).strip()
    if len(flat) <= limit:
        return flat
    head = flat[:limit]
    ends = [m.end() for m in _SENTENCE_END.finditer(head)]
    if ends and ends[-1] >= limit // 3:
        return head[: ends[-1]].rstrip()
    cut = head.rfind(" ")
    return head[:cut].rstrip() if cut > 0 else head
