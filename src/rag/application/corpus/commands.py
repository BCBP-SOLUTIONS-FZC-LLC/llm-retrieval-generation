"""Commands and queries of the corpus use cases."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from uuid import UUID

from rag.domain.shared.identity import RequestIdentity


@dataclass(frozen=True, slots=True)
class ResolveCollectionCommand:
    """C-I1. ``tender_restricted`` is advisory, one-way and an input to creation only."""

    tenant_id: UUID
    tender_id: UUID
    tender_restricted: bool | None = None


@dataclass(frozen=True, slots=True)
class ActivateGenerationCommand:
    """C-I4. ``record_version`` is the collection's (a generation has none, §5.7.16)."""

    tenant_id: UUID
    collection_id: UUID
    generation: int
    record_version: int


class MigrationOutcome(StrEnum):
    """C-I8 ``result``."""

    COMPLETE = "complete"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class MigrationResultCommand:
    """C-I8; ``points_copied`` is required on ``complete``, ``reason`` on ``failed``."""

    tenant_id: UUID
    collection_id: UUID
    migration_id: UUID
    result: MigrationOutcome
    points_copied: int | None = None
    reason: str | None = None


@dataclass(frozen=True, slots=True)
class PutRetentionCommand:
    """P-6. No ``record_version`` means create."""

    identity: RequestIdentity
    collection_id: UUID
    retain_days: int
    legal_hold: bool
    record_version: int | None = None


@dataclass(frozen=True, slots=True)
class ListCollectionsQuery:
    """P-1."""

    identity: RequestIdentity
    limit: int
    offset: int


@dataclass(frozen=True, slots=True)
class CollectionQuery:
    """P-2, and the collection lookup of P-3."""

    identity: RequestIdentity
    collection_id: UUID


@dataclass(frozen=True, slots=True)
class ListGrantsQuery:
    """P-3."""

    identity: RequestIdentity
    collection_id: UUID
    limit: int
    offset: int
