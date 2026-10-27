"""Result shapes of the corpus use cases (no behaviour)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID


@dataclass(frozen=True, slots=True)
class PlacementDto:
    """C-I1 / C-I2 body: where a document is written, and the generation to stamp."""

    collection_id: UUID
    physical_collection: str
    shard_key: str
    type: str
    knowledge_level: str
    status: str
    active_index_generation: int
    purge_epoch: int
    restricted: bool
    record_version: int


@dataclass(frozen=True, slots=True)
class ResolveResultDto:
    """C-I1: the placement and whether this call created the row (201 against 200)."""

    placement: PlacementDto
    created: bool


@dataclass(frozen=True, slots=True)
class CollectionDto:
    """P-1 / P-2 summary: no physical collection name for a tenant caller."""

    id: UUID
    type: str
    knowledge_level: str
    tender_id: UUID
    status: str
    restricted: bool
    active_index_generation: int
    weight_override: float | None
    promoted_from: UUID | None
    tender_closed_at: datetime | None
    record_version: int
    created_at: datetime | None
    updated_at: datetime | None


@dataclass(frozen=True, slots=True)
class CollectionPageDto:
    """P-1 body."""

    collections: list[CollectionDto]
    limit: int
    offset: int


@dataclass(frozen=True, slots=True)
class ActivationDto:
    """C-I4 body; ``previous`` is ``None`` on a collection's first activation (LD-94)."""

    active_index_generation: int
    previous: int | None
    record_version: int


@dataclass(frozen=True, slots=True)
class MigrationResultDto:
    """C-I8 body."""

    collection_id: UUID
    status: str
    record_version: int


@dataclass(frozen=True, slots=True)
class PurgeEpochDto:
    """C-I6 body."""

    purge_epoch: int


@dataclass(frozen=True, slots=True)
class RetentionDto:
    """C-I5 / P-6 body."""

    collection_id: UUID
    retain_days: int
    legal_hold: bool
    record_version: int
    updated_at: datetime | None


@dataclass(frozen=True, slots=True)
class PutRetentionResultDto:
    """P-6: the stored policy and whether the call created it (201 against 200)."""

    policy: RetentionDto
    created: bool


@dataclass(frozen=True, slots=True)
class GrantEntryDto:
    """One P-3 entry."""

    subject_type: str
    subject_id: UUID
    level: str
    projected_at: datetime
    expires_at: datetime | None
    source: str


@dataclass(frozen=True, slots=True)
class GrantPageDto:
    """P-3 body. ``authoritative`` is always false: this service mints no grant (CG-2)."""

    entries: list[GrantEntryDto]
    limit: int
    offset: int
    as_of: datetime | None
    bound_seconds: int
    authoritative: bool = False


@dataclass(frozen=True, slots=True)
class ModelDto:
    """One C-M1 model."""

    id: str
    residency: str
    task_classes: list[str]
    plans: list[str]


@dataclass(frozen=True, slots=True)
class ModelCatalogueDto:
    """C-M1 body."""

    models: list[ModelDto]
    as_of: datetime
