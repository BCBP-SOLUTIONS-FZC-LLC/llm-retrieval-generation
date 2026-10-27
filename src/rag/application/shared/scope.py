"""Scope authorization port shared by every route that re-resolves scope on read (API-10)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol
from uuid import UUID

from rag.domain.shared.enums import GrantLevel
from rag.domain.shared.identity import RequestIdentity


@dataclass(frozen=True, slots=True)
class ScopeTarget:
    """What a read touches: a tender (routing only, SCOPE-7) and/or named collections.

    A free-standing answer has no tender and no collections: its scope is the caller's
    tenant-wide and reference collections, which the implementation resolves.
    """

    tender_id: UUID | None = None
    collection_ids: tuple[UUID, ...] = ()


class ScopeAuthorizer(Protocol):
    """Re-resolves the caller's current grants against ``target`` on every read."""

    async def require_level(
        self, identity: RequestIdentity, target: ScopeTarget, level: GrantLevel
    ) -> None:
        """Return if the caller holds at least ``level`` on everything in ``target``.

        Raises ``DomainError(GRANT_LEVEL_INSUFFICIENT)`` where the grant is too low or revoked
        and ``DomainError(SCOPE_STORE_UNAVAILABLE)`` where grants cannot be read (fail closed).
        """
        ...
