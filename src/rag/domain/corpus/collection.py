"""``Collection``: identity derivation (COL-11), status machine (§4.7.1, COL-10, COL-12)."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime
from uuid import UUID, uuid5

from rag.domain.corpus.errors import InvalidTransition
from rag.domain.shared.enums import CollectionStatus, CollectionType

COLLECTION_NAMESPACE = UUID("8d9d8e69-7546-4011-bf89-15ef4c822386")
"""UUIDv5 namespace of ``collections.id`` (§17 ``COLLECTION_NAMESPACE``). Never change it."""

TENANT_WIDE = UUID("00000000-0000-7000-8000-4c4c4d000001")
"""``tender_id`` of a ``tenant`` collection (§4.10)."""
REFERENCE = UUID("00000000-0000-7000-8000-4c4c4d000002")
"""``tender_id`` of a ``reference`` collection."""
PLATFORM = UUID("00000000-0000-7000-8000-4c4c4d000003")
"""``tenant_id`` of a ``reference`` collection."""

RETRIEVABLE = frozenset({CollectionStatus.ACTIVE})
WRITABLE = frozenset(
    {CollectionStatus.PROVISIONING, CollectionStatus.ACTIVE, CollectionStatus.MIGRATION_FAILED}
)
_FREEZABLE = WRITABLE
KNOWLEDGE_LEVEL = {
    CollectionType.TENDER: "TENDER",
    CollectionType.TENANT: "TENANT_WIDE",
    CollectionType.REFERENCE: "REFERENCE",
}


def derive_collection_id(
    type_: CollectionType,
    tenant_id: UUID,
    tender_id: UUID,
    restricted: bool,
    namespace: UUID = COLLECTION_NAMESPACE,
) -> UUID:
    """``UUIDv5(ns, "{type}:{tenant_id}:{tender_id}:{restricted}")``, lower case (COL-11)."""
    name = f"{type_.value}:{tenant_id}:{tender_id}:{'true' if restricted else 'false'}"
    return uuid5(namespace, name)


def collection_type_for(tender_id: UUID) -> CollectionType:
    """The type a ``tender_id`` denotes: the sentinels name the non-tender types (§4.10)."""
    if tender_id == TENANT_WIDE:
        return CollectionType.TENANT
    if tender_id == REFERENCE:
        return CollectionType.REFERENCE
    return CollectionType.TENDER


def derive_placement(
    type_: CollectionType,
    tenant_id: UUID,
    tender_id: UUID,
    restricted: bool,
) -> tuple[str, str]:
    """``(physical_collection, shard_key)``. ``restricted`` decides placement (COL-1, MD-19).

    The LLD fixes the rule (ordinary: the tenant's own physical collection, split by shard key;
    restricted: a collection of its own) but not the spelling, which is Ingestion's to confirm.
    """
    tenant = tenant_id.hex
    if type_ is CollectionType.REFERENCE:
        return "reference", "reference"
    if type_ is CollectionType.TENANT:
        return f"t_{tenant}", f"tenant_{tenant}"
    if restricted:
        return f"t_{tenant}__tender__{tender_id.hex}", f"tender_{tender_id.hex}"
    return f"t_{tenant}", f"tender_{tender_id.hex}"


@dataclass(frozen=True, slots=True)
class Collection:
    """One ``collections`` row. Transitions return a new value and never mutate."""

    id: UUID
    tenant_id: UUID
    type: CollectionType
    tender_id: UUID
    restricted: bool
    physical_collection: str
    shard_key: str
    status: CollectionStatus
    active_index_generation: int = 1
    purge_epoch: int = 0
    status_before_freeze: CollectionStatus | None = None
    weight_override: float | None = None
    owning_departments: tuple[UUID, ...] = ()
    promoted_from: UUID | None = None
    restricted_announced_at: datetime | None = None
    tender_closed_at: datetime | None = None
    migration_id: UUID | None = None
    record_version: int = 1
    created_at: datetime | None = None
    updated_at: datetime | None = None

    @classmethod
    def create(
        cls,
        *,
        tenant_id: UUID,
        tender_id: UUID,
        restricted: bool,
        namespace: UUID = COLLECTION_NAMESPACE,
        promoted_from: UUID | None = None,
        migration_id: UUID | None = None,
    ) -> Collection:
        """A new row with derived id and placement (COL-11, COL-1).

        A ``tender`` row starts ``provisioning``; a ``tenant`` or ``reference`` row starts
        ``active`` (§5.7.17). A promotion target is always ``restricted`` (COL-7).
        """
        type_ = collection_type_for(tender_id)
        physical, shard = derive_placement(type_, tenant_id, tender_id, restricted)
        tenant = PLATFORM if type_ is CollectionType.REFERENCE else tenant_id
        return cls(
            id=derive_collection_id(type_, tenant, tender_id, restricted, namespace),
            tenant_id=tenant,
            type=type_,
            tender_id=tender_id,
            restricted=restricted,
            physical_collection=physical,
            shard_key=shard,
            status=(
                CollectionStatus.PROVISIONING
                if type_ is CollectionType.TENDER
                else CollectionStatus.ACTIVE
            ),
            promoted_from=promoted_from,
            migration_id=migration_id,
        )

    @property
    def retrievable(self) -> bool:
        """Retrieval admits ``active`` only (COL-10)."""
        return self.status in RETRIEVABLE

    @property
    def writable(self) -> bool:
        """Writes admit ``provisioning``, ``active`` and ``migration_failed`` (COL-10)."""
        return self.status in WRITABLE

    @property
    def knowledge_level(self) -> str:
        """``TENDER`` / ``TENANT_WIDE`` / ``REFERENCE`` (LD-93)."""
        return KNOWLEDGE_LEVEL[self.type]

    @property
    def is_promotion_target(self) -> bool:
        """A row inserted by a promotion (COL-7)."""
        return self.promoted_from is not None

    def _move(self, allowed: frozenset[CollectionStatus], to: CollectionStatus) -> Collection:
        if self.status not in allowed:
            raise InvalidTransition(f"collection {self.status} -> {to} is not a transition")
        return replace(self, status=to, status_before_freeze=None)

    def freeze(self) -> Collection:
        """Tenant suspension: remember the interrupted status (COL-12)."""
        if self.status not in _FREEZABLE:
            raise InvalidTransition(f"collection {self.status} cannot be frozen")
        return replace(self, status=CollectionStatus.FROZEN, status_before_freeze=self.status)

    def restore(self) -> Collection:
        """Reactivation: back to the recorded status, never to ``active`` blindly (COL-12)."""
        if self.status is not CollectionStatus.FROZEN or self.status_before_freeze is None:
            raise InvalidTransition(f"collection {self.status} is not frozen")
        return replace(self, status=self.status_before_freeze, status_before_freeze=None)

    def begin_purge(self) -> Collection:
        """Tender purge, offboarding or deletion; terminal, the row stays (COL-13).

        Deviation: ``provisioning`` may also purge (a tender that never activated), which the
        LLD's diagram does not draw but a tender purge needs.
        """
        return self._move(_FREEZABLE | {CollectionStatus.FROZEN}, CollectionStatus.PURGING)

    def supersede(self) -> Collection:
        """A promotion cutover retired this row (COL-7)."""
        return self._move(frozenset({CollectionStatus.ACTIVE}), CollectionStatus.SUPERSEDED)

    def activate(self) -> Collection:
        """First activation or promotion cutover: ``provisioning`` becomes ``active`` (IG-4)."""
        return self._move(frozenset({CollectionStatus.PROVISIONING}), CollectionStatus.ACTIVE)

    def fail_migration(self) -> Collection:
        """C-I8 reported a failed promotion copy."""
        return self._move(
            frozenset({CollectionStatus.PROVISIONING}), CollectionStatus.MIGRATION_FAILED
        )

    def retry_migration(self) -> Collection:
        """An operator retries the promotion."""
        return self._move(
            frozenset({CollectionStatus.MIGRATION_FAILED}), CollectionStatus.PROVISIONING
        )

    def with_active_generation(self, generation: int) -> Collection:
        """Point searches at ``generation`` (C-I4)."""
        return replace(self, active_index_generation=generation)

    def announce_restricted(self, at: datetime) -> Collection:
        """Set ``restricted_announced_at`` once (COL-8)."""
        if self.restricted_announced_at is not None:
            return self
        return replace(self, restricted_announced_at=at)

    def close_tender(self, at: datetime) -> Collection:
        """Record a terminal tender outcome once (COL-14)."""
        if self.tender_closed_at is not None:
            return self
        return replace(self, tender_closed_at=at)


def pick_resolvable(rows: list[Collection]) -> Collection | None:
    """C-I1's lookup over the non-superseded rows of one ``(tenant, tender, type)`` (§5.7.16).

    ``active`` over ``provisioning``; a ``frozen`` or ``purging`` row is returned so the caller
    can refuse it; ``migration_failed`` is never the answer; ``superseded`` is skipped (COL-9).
    The lookup ignores ``restricted`` on purpose.
    """
    for status in (
        CollectionStatus.ACTIVE,
        CollectionStatus.PROVISIONING,
        CollectionStatus.FROZEN,
        CollectionStatus.PURGING,
    ):
        for row in rows:
            if row.status is status:
                return row
    return None
