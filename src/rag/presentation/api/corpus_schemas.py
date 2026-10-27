"""Request and response bodies of the corpus routes (§5.7.15, §5.7.16, §5.7.18)."""

from __future__ import annotations

from dataclasses import asdict
from datetime import datetime
from typing import Any, Literal, Self
from uuid import UUID

from pydantic import BaseModel, Field, model_validator

from rag.application.corpus.dtos import (
    ActivationDto,
    CollectionDto,
    CollectionPageDto,
    GrantPageDto,
    MigrationResultDto,
    ModelCatalogueDto,
    PlacementDto,
    PurgeEpochDto,
    RetentionDto,
)


class ResolveRequest(BaseModel):
    """C-I1. ``tender_id`` is required; ``tender_restricted`` is optional and advisory."""

    tender_id: UUID
    tender_restricted: bool | None = None


class PlacementResponse(BaseModel):
    """C-I1 / C-I2 body (200 and 201 carry the same one)."""

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

    @classmethod
    def of(cls, dto: PlacementDto) -> Self:
        """From the use case's DTO."""
        return cls(**vars_of(dto))


class ActivateRequest(BaseModel):
    """C-I4: the collection's ``record_version``."""

    record_version: int = Field(ge=1)


class ActivateResponse(BaseModel):
    """C-I4 body; ``previous`` is always present and null on a first activation."""

    active_index_generation: int
    previous: int | None
    record_version: int

    @classmethod
    def of(cls, dto: ActivationDto) -> Self:
        """From the use case's DTO."""
        return cls(**vars_of(dto))


class PurgeEpochResponse(BaseModel):
    """C-I6 body."""

    purge_epoch: int

    @classmethod
    def of(cls, dto: PurgeEpochDto) -> Self:
        """From the use case's DTO."""
        return cls(purge_epoch=dto.purge_epoch)


class RetentionResponse(BaseModel):
    """C-I5 body."""

    collection_id: UUID
    retain_days: int
    legal_hold: bool
    record_version: int
    updated_at: datetime | None

    @classmethod
    def of(cls, dto: RetentionDto) -> Self:
        """From the use case's DTO."""
        return cls(**vars_of(dto))


class PutRetentionRequest(BaseModel):
    """P-6. No ``record_version`` creates the policy (LD-76)."""

    retain_days: int = Field(gt=0)
    legal_hold: bool = False
    record_version: int | None = Field(default=None, ge=1)


class PutRetentionResponse(RetentionResponse):
    """P-6 body. ``applied`` is false until the retention sweep exists (rev 2.67)."""

    applied: Literal[False] = False


class MigrationResultRequest(BaseModel):
    """C-I8: ``points_copied`` on ``complete``, ``reason`` on ``failed``."""

    migration_id: UUID
    result: Literal["complete", "failed"]
    points_copied: int | None = Field(default=None, ge=0)
    reason: str | None = Field(default=None, min_length=1, max_length=256)

    @model_validator(mode="after")
    def _result_fields(self) -> Self:
        if self.result == "complete" and self.points_copied is None:
            raise ValueError("points_copied is required when result is complete")
        if self.result == "failed" and self.reason is None:
            raise ValueError("reason is required when result is failed")
        return self


class MigrationResultResponse(BaseModel):
    """C-I8 body."""

    collection_id: UUID
    status: str
    record_version: int

    @classmethod
    def of(cls, dto: MigrationResultDto) -> Self:
        """From the use case's DTO."""
        return cls(**vars_of(dto))


class CollectionResponse(BaseModel):
    """P-2 body and a P-1 entry: placement without the physical collection name."""

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

    @classmethod
    def of(cls, dto: CollectionDto) -> Self:
        """From the use case's DTO."""
        return cls(**vars_of(dto))


class CollectionListResponse(BaseModel):
    """P-1 body."""

    collections: list[CollectionResponse]
    limit: int
    offset: int

    @classmethod
    def of(cls, dto: CollectionPageDto) -> Self:
        """From the use case's DTO."""
        return cls(
            collections=[CollectionResponse.of(c) for c in dto.collections],
            limit=dto.limit,
            offset=dto.offset,
        )


class GrantEntryResponse(BaseModel):
    """One P-3 entry."""

    subject_type: str
    subject_id: UUID
    level: str
    projected_at: datetime
    expires_at: datetime | None
    source: str


class ProjectionFreshness(BaseModel):
    """Age of the newest recorded overlay answer, and the bound the live path guarantees."""

    as_of: datetime | None
    bound_seconds: int


class GrantListResponse(BaseModel):
    """P-3 body. ``authoritative`` is a literal false: this service mints no grant (CG-2)."""

    entries: list[GrantEntryResponse]
    limit: int
    offset: int
    projection_freshness: ProjectionFreshness
    authoritative: Literal[False] = False

    @classmethod
    def of(cls, dto: GrantPageDto) -> Self:
        """From the use case's DTO."""
        return cls(
            entries=[GrantEntryResponse(**vars_of(e)) for e in dto.entries],
            limit=dto.limit,
            offset=dto.offset,
            projection_freshness=ProjectionFreshness(
                as_of=dto.as_of, bound_seconds=dto.bound_seconds
            ),
        )


class ModelResponse(BaseModel):
    """One C-M1 model."""

    id: str
    residency: str
    task_classes: list[str]
    plans: list[str]


class ModelCatalogueResponse(BaseModel):
    """C-M1 body."""

    models: list[ModelResponse]
    as_of: datetime

    @classmethod
    def of(cls, dto: ModelCatalogueDto) -> Self:
        """From the use case's DTO."""
        return cls(models=[ModelResponse(**vars_of(m)) for m in dto.models], as_of=dto.as_of)


def vars_of(dto: Any) -> dict[str, Any]:
    """A flat DTO's fields as keyword arguments."""
    return asdict(dto)
