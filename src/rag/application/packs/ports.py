"""Pack resolution port (LLD §22.3, §22.9): Tender is the pack authority; we pull."""

from __future__ import annotations

from typing import Protocol
from uuid import UUID

from rag.domain.packs.profile import PackBundle


class PackResolver(Protocol):
    """Resolves the Domain Profile and Language Pack a request pins.

    Cache-fronted (§11.1, RET-3): a hit never leaves the process; a miss goes to Tender with a
    2 s timeout, two retries and a breaker. ``pinned_version`` re-reads a version a run already
    pinned (``ASYNC-3``). Raises ``PackUnavailable`` where Tender cannot answer; the caller
    falls back to ``PLATFORM_DEFAULT_BUNDLE`` with a marker (LD-14).
    """

    async def resolve(
        self,
        tenant_id: UUID,
        *,
        domain_profile_ref: str | None = None,
        language: str | None = None,
        pinned_version: str | None = None,
    ) -> PackBundle:
        """The bundle for ``(tenant, profile ref, language)`` at the current or pinned version."""
        ...
