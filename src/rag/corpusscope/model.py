"""Value objects of ``corpusscope``: sentinels, levels, the collection row the resolver reads.

This package is vendored (Ingestion copies it, LLD §3.1, §22.4), so it depends on the standard
library, ``pydantic`` and ``qdrant_client`` only. The sentinels and the level order below are
copies of the service's domain constants; ``tests/unit/test_corpusscope_constants.py`` asserts
they agree.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import IntEnum
from uuid import UUID

TENANT_WIDE = UUID("00000000-0000-7000-8000-4c4c4d000001")
"""``tender_id`` of a ``tenant`` collection (§4.10)."""
REFERENCE = UUID("00000000-0000-7000-8000-4c4c4d000002")
"""``tender_id`` of a ``reference`` collection."""
PLATFORM = UUID("00000000-0000-7000-8000-4c4c4d000003")
"""``tenant_id`` of a ``reference`` collection."""

TYPE_TENDER, TYPE_TENANT, TYPE_REFERENCE = "tender", "tenant", "reference"
STATUS_ACTIVE = "active"  # the only retrievable status (COL-10, SCOPE-11)


class Level(IntEnum):
    """Grant level; ordered so ``min()`` is the clamp (SCOPE-8). Values map to the enum labels."""

    SEARCH = 1
    ANSWER = 2
    GENERATE = 3

    @property
    def label(self) -> str:
        """The ``grant_level`` enum label."""
        return self.name.lower()

    @classmethod
    def parse(cls, label: str) -> Level:
        """The level for an enum label (``ValueError`` where unknown)."""
        return cls[label.upper()]


@dataclass(frozen=True, slots=True)
class Principal:
    """Who asks, from the gateway headers: tenant, user, roles, departments."""

    tenant_id: UUID
    principal_id: UUID | None = None
    roles: frozenset[str] = frozenset()
    departments: tuple[UUID, ...] = ()


@dataclass(frozen=True, slots=True)
class ScopeRequest:
    """``ScopeRequest`` of §5.5. ``tender_id`` routes and filters, it never authorizes (SCOPE-7)."""

    tender_id: UUID | None = None
    collection_ids: tuple[UUID, ...] = ()
    collection_types: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class CollectionRow:
    """One ``collections`` row plus its active generation, as the resolver needs it.

    ``active_generation`` is ``None`` where ``index_generations`` has no ``active`` row
    (SCOPE-12); the column ``collections.active_index_generation`` is never ``NULL`` and is not
    evidence of an activation.
    """

    id: UUID
    tenant_id: UUID
    type: str
    tender_id: UUID
    restricted: bool
    status: str
    physical_collection: str
    shard_key: str
    purge_epoch: int
    active_generation: int | None
    weight_override: float | None = None
    owning_departments: tuple[UUID, ...] = ()
    restricted_announced_at: datetime | None = field(default=None)

    @property
    def is_restricted(self) -> bool:
        """Not ordinary: ``restricted`` or an announced restriction (COL-8)."""
        return self.type == TYPE_TENDER and (
            self.restricted or self.restricted_announced_at is not None
        )
