"""The vendored library's copies of domain constants agree with the domain."""

from __future__ import annotations

from rag.corpusscope import PLATFORM, REFERENCE, TENANT_WIDE, Level
from rag.domain.corpus import collection as domain
from rag.domain.shared.enums import GrantLevel


def test_sentinels_match_the_domain() -> None:
    """Three literals, shared with Ingestion's rows and payloads (§4.10)."""
    assert (TENANT_WIDE, REFERENCE, PLATFORM) == (
        domain.TENANT_WIDE,
        domain.REFERENCE,
        domain.PLATFORM,
    )


def test_levels_match_the_domain_order_and_labels() -> None:
    """The clamp is ``min()`` over the same order (SCOPE-8, LD-21)."""
    assert [lvl.label for lvl in Level] == [g.value for g in GrantLevel]
    assert [lvl.value for lvl in Level] == [g.rank + 1 for g in GrantLevel]
