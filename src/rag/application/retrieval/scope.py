"""The retrieval module's scope port: resolve a ``ResolvedScope`` at a required level."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol
from uuid import UUID

from rag.domain.retrieval.scope import ResolvedScope
from rag.domain.shared.enums import GrantLevel
from rag.domain.shared.identity import RequestIdentity


@dataclass(frozen=True, slots=True)
class ScopeSpec:
    """``ScopeRequest`` of §5.5. ``tender_id`` routes and filters, it never authorizes (API-3)."""

    tender_id: UUID | None = None
    collection_ids: tuple[UUID, ...] = ()
    collection_types: tuple[str, ...] = ()


class ScopeProvider(Protocol):
    """Stage 0 (§6.2). Implemented over ``corpusscope`` in the infrastructure layer."""

    async def resolve(
        self, identity: RequestIdentity, spec: ScopeSpec, required: GrantLevel
    ) -> ResolvedScope:
        """The scope at ``required`` level.

        Raises ``DomainError``: 404 ``COLLECTION_NOT_FOUND`` (nothing in scope, SCOPE-6), 403
        ``GRANT_LEVEL_INSUFFICIENT``, 503 ``SCOPE_STORE_UNAVAILABLE`` (CG-4).
        """
        ...
