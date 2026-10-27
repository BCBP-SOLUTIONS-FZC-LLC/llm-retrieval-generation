"""The resolved scope as the application sees it.

The ``handle`` is opaque to every layer but the infrastructure adapter that issued it (it is
``corpusscope``'s ``ScopeToken``).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

from rag.domain.shared.enums import GrantLevel


@dataclass(frozen=True, slots=True)
class ScopedCollectionInfo:
    """One collection in scope with what retrieval records of it (§4.4.2, RSC-3)."""

    id: UUID
    type: str
    level: GrantLevel
    weight: Decimal
    index_generation: int | None
    purge_epoch: int

    @property
    def empty(self) -> bool:
        """No activated generation: an empty corpus, never searched (SCOPE-12)."""
        return self.index_generation is None


@dataclass(frozen=True, slots=True)
class ResolvedScope:
    """A resolved scope (``ScopeToken`` seen from the application layer)."""

    tenant_id: UUID
    principal_id: UUID | None
    collections: tuple[ScopedCollectionInfo, ...]
    tender_ids: tuple[UUID, ...]
    level: GrantLevel
    clamped: bool
    resolution_hash: str
    resolved_at: datetime
    expires_at: datetime
    handle: Any = field(default=None, repr=False, compare=False)

    @property
    def searchable(self) -> tuple[ScopedCollectionInfo, ...]:
        """Collections with an active generation."""
        return tuple(c for c in self.collections if not c.empty)

    @property
    def empty_collection_ids(self) -> tuple[UUID, ...]:
        """Named in ``empty_collections`` on a gap report (SCOPE-12)."""
        return tuple(c.id for c in self.collections if c.empty)

    def weights(self) -> dict[UUID, Decimal]:
        """Collection weight by id, as applied (RSC-3)."""
        return {c.id: c.weight for c in self.collections}

    def generation_epoch_digest(self) -> str:
        """Generation and purge epoch per collection: the retrieval-set cache key part (§11.3)."""
        return ",".join(
            f"{c.id}:{c.index_generation}:{c.purge_epoch}"
            for c in sorted(self.collections, key=lambda c: str(c.id))
        )

    def filter_set(self) -> dict[str, Any]:
        """What ``retrieval_sets.filter_set`` records: the compound each search was built from."""
        return {
            "tenant_id": str(self.tenant_id),
            "tender_ids": [str(t) for t in self.tender_ids],
            "collection_ids": [str(c.id) for c in self.collections],
            "index_generation_per_collection": {
                str(c.id): c.index_generation for c in self.collections
            },
            "is_current": True,
        }
