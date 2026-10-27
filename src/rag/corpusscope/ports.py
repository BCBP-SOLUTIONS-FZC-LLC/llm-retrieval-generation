"""Ports of ``corpusscope``: collection store, TAC-4, scope cache."""

from __future__ import annotations

from typing import Protocol
from uuid import UUID

from rag.corpusscope.model import CollectionRow


class CollectionStore(Protocol):
    """Reads ``collections`` and active generations from the PRIMARY (SCOPE-10)."""

    async def load(self, tenant_id: UUID) -> list[CollectionRow]:
        """Every row of ``tenant_id`` plus the PLATFORM reference rows, whatever their status.

        Runs in one read transaction under RLS with the tenant GUC bound. Raises on an
        unreachable store; the resolver turns that into ``ScopeStoreUnavailable``.
        """
        ...


class TenderAccess(Protocol):
    """TAC-4: the overlay grant for ``(tenant, tender, principal)`` (CG-1, CG-11)."""

    async def level(self, tenant_id: UUID, tender_id: UUID, principal_id: UUID) -> str | None:
        """``view`` / ``edit`` / ``approve``, or ``None`` for a successful no-access answer (CG-5).

        Raises where unreachable or unintelligible.
        """
        ...


class ScopeCache(Protocol):
    """A string cache with a TTL. A failing cache must behave as a miss, never raise."""

    async def get(self, key: str) -> str | None:
        """The value, or ``None``."""
        ...

    async def set(self, key: str, value: str, ttl_seconds: int) -> None:
        """Store ``value`` for ``ttl_seconds``."""
        ...
