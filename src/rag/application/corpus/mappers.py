"""Entity to DTO mapping."""

from __future__ import annotations

from rag.application.corpus.dtos import CollectionDto, PlacementDto
from rag.domain.corpus.collection import Collection


def to_placement(c: Collection, record_version: int | None = None) -> PlacementDto:
    """C-I1 / C-I2 shape."""
    return PlacementDto(
        collection_id=c.id,
        physical_collection=c.physical_collection,
        shard_key=c.shard_key,
        type=c.type.value,
        knowledge_level=c.knowledge_level,
        status=c.status.value,
        active_index_generation=c.active_index_generation,
        purge_epoch=c.purge_epoch,
        restricted=c.restricted,
        record_version=c.record_version if record_version is None else record_version,
    )


def to_collection_dto(c: Collection) -> CollectionDto:
    """P-1 / P-2 shape."""
    return CollectionDto(
        id=c.id,
        type=c.type.value,
        knowledge_level=c.knowledge_level,
        tender_id=c.tender_id,
        status=c.status.value,
        restricted=c.restricted,
        active_index_generation=c.active_index_generation,
        weight_override=c.weight_override,
        promoted_from=c.promoted_from,
        tender_closed_at=c.tender_closed_at,
        record_version=c.record_version,
        created_at=c.created_at,
        updated_at=c.updated_at,
    )
