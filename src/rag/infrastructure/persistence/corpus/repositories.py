"""Corpus repository ports over the ``pgcommon`` transaction.

Every method takes the transaction-bound connection ``pgcommon.run_in_tx`` hands the use case,
so RLS (the tenant GUC bound for that transaction) applies to each statement. Statements that
write on behalf of a tenant also filter on ``tenant_id`` explicitly; RLS is the backstop.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from datetime import datetime
from typing import Any
from uuid import UUID

from rag.domain.corpus.collection import Collection
from rag.domain.corpus.grant import CollectionGrant
from rag.domain.corpus.index_generation import IndexGeneration
from rag.domain.corpus.retention import RetentionPolicy
from rag.domain.corpus.tenant_lifecycle import TenantLifecycle, TenantState
from rag.domain.shared.enums import (
    CollectionStatus,
    CollectionType,
    GrantLevel,
    GrantSubjectType,
    IndexGenerationStatus,
)

_COLLECTION = (
    "id, tenant_id, type, tender_id, restricted, physical_collection, shard_key, "
    "active_index_generation, purge_epoch, status, status_before_freeze, weight_override, "
    "owning_departments, promoted_from, restricted_announced_at, tender_closed_at, "
    "migration_id, record_version, created_at, updated_at"
)
_LOCK = " FOR UPDATE"


class CollectionSlotConflict(Exception):  # noqa: N818 - named for the conflict, like the DB error
    """Another row holds this tender's slot under a different id (``uq_coll_tender``, COL-9).

    ``INSERT ... ON CONFLICT DO NOTHING`` swallows every unique violation, so a conflict that is
    not the concurrent first-document race on the derived id is raised here. It carries what
    Postgres' own error would (``sqlstate`` 23505 and the constraint name), which is exactly what
    ``pgcommon.is_unique_violation`` and ``pgcommon.constraint_name`` read, so callers classify it
    the same way and this layer never imports a database driver.
    """

    sqlstate = "23505"
    constraint_name = "uq_coll_tender"

    def __init__(self) -> None:
        super().__init__(self.constraint_name)


def _count(status: str) -> int:
    """Rows affected, from a command tag such as ``UPDATE 3``."""
    return int(status.rsplit(" ", 1)[-1])


def _collection(r: Any) -> Collection:
    before = r["status_before_freeze"]
    weight = r["weight_override"]
    return Collection(
        id=r["id"],
        tenant_id=r["tenant_id"],
        type=CollectionType(r["type"]),
        tender_id=r["tender_id"],
        restricted=r["restricted"],
        physical_collection=r["physical_collection"],
        shard_key=r["shard_key"],
        status=CollectionStatus(r["status"]),
        active_index_generation=r["active_index_generation"],
        purge_epoch=r["purge_epoch"],
        status_before_freeze=None if before is None else CollectionStatus(before),
        weight_override=None if weight is None else float(weight),
        owning_departments=tuple(r["owning_departments"]),
        promoted_from=r["promoted_from"],
        restricted_announced_at=r["restricted_announced_at"],
        tender_closed_at=r["tender_closed_at"],
        migration_id=r["migration_id"],
        record_version=r["record_version"],
        created_at=r["created_at"],
        updated_at=r["updated_at"],
    )


class PgCollectionRepository:
    """``collections``."""

    async def get(self, tx: Any, collection_id: UUID, *, lock: bool = False) -> Collection | None:
        """One row by id."""
        row = await tx.fetchrow(
            f"SELECT {_COLLECTION} FROM collections WHERE id = $1{_LOCK if lock else ''}",
            collection_id,
        )
        return None if row is None else _collection(row)

    async def find_by_tender(
        self,
        tx: Any,
        tenant_id: UUID,
        tender_id: UUID,
        type_: CollectionType,
        *,
        lock: bool = False,
    ) -> list[Collection]:
        """Non-superseded rows of one ``(tenant, tender, type)``."""
        rows = await tx.fetch(
            f"SELECT {_COLLECTION} FROM collections "
            "WHERE tenant_id = $1 AND tender_id = $2 AND type = $3 AND status <> 'superseded' "
            f"ORDER BY created_at, id{_LOCK if lock else ''}",
            tenant_id,
            tender_id,
            type_.value,
        )
        return [_collection(r) for r in rows]

    async def list_by_tenant(self, tx: Any, tenant_id: UUID) -> list[Collection]:
        """The tenant's own rows (the explicit filter excludes the PLATFORM reference row)."""
        rows = await tx.fetch(
            f"SELECT {_COLLECTION} FROM collections WHERE tenant_id = $1 "
            f"ORDER BY created_at, id{_LOCK}",
            tenant_id,
        )
        return [_collection(r) for r in rows]

    async def list_stalled_promotions(self, tx: Any, before: datetime) -> list[Collection]:
        """Promotion targets stuck in ``provisioning`` (the caller's tenant, by RLS)."""
        rows = await tx.fetch(
            f"SELECT {_COLLECTION} FROM collections WHERE status = 'provisioning' "
            f"AND promoted_from IS NOT NULL AND created_at < $1 ORDER BY created_at, id{_LOCK}",
            before,
        )
        return [_collection(r) for r in rows]

    async def list_by_ids(
        self, tx: Any, ids: Sequence[UUID], *, limit: int, offset: int
    ) -> list[Collection]:
        """A page of the named rows."""
        rows = await tx.fetch(
            f"SELECT {_COLLECTION} FROM collections WHERE id = ANY($1::uuid[]) "
            "ORDER BY created_at, id LIMIT $2 OFFSET $3",
            list(ids),
            limit,
            offset,
        )
        return [_collection(r) for r in rows]

    async def insert(self, tx: Any, collection: Collection) -> bool:
        """Insert; the derived primary key decides a race (COL-11)."""
        c = collection
        inserted = await tx.fetchval(
            "INSERT INTO collections (id, tenant_id, type, tender_id, restricted, "
            "physical_collection, shard_key, active_index_generation, purge_epoch, status, "
            "status_before_freeze, weight_override, owning_departments, promoted_from, "
            "restricted_announced_at, tender_closed_at, migration_id) "
            "VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, $15, $16, $17) "
            "ON CONFLICT DO NOTHING RETURNING id",
            c.id,
            c.tenant_id,
            c.type.value,
            c.tender_id,
            c.restricted,
            c.physical_collection,
            c.shard_key,
            c.active_index_generation,
            c.purge_epoch,
            c.status.value,
            None if c.status_before_freeze is None else c.status_before_freeze.value,
            c.weight_override,
            list(c.owning_departments),
            c.promoted_from,
            c.restricted_announced_at,
            c.tender_closed_at,
            c.migration_id,
        )
        if inserted is None and not await tx.fetchval(
            "SELECT 1 FROM collections WHERE id = $1", c.id
        ):
            # Another row holds this tender's slot under a different id (uq_coll_tender): not a race
            # on the derived key, so it is a real conflict (COL-9).
            raise CollectionSlotConflict
        return inserted is not None

    async def update(self, tx: Any, collection: Collection, expected_version: int) -> int | None:
        """Write the columns a transition changes, guarded by ``record_version`` (§14.1).

        ``purge_epoch`` is deliberately not written: only ``bump_purge_epoch`` changes it, so a
        stale read can never roll an epoch back.
        """
        c = collection
        version = await tx.fetchval(
            "UPDATE collections SET status = $3, status_before_freeze = $4, "
            "active_index_generation = $5, restricted_announced_at = $6, "
            "tender_closed_at = $7, migration_id = $8 "
            "WHERE id = $1 AND record_version = $2 RETURNING record_version",
            c.id,
            expected_version,
            c.status.value,
            None if c.status_before_freeze is None else c.status_before_freeze.value,
            c.active_index_generation,
            c.restricted_announced_at,
            c.tender_closed_at,
            c.migration_id,
        )
        return None if version is None else int(version)

    async def bump_purge_epoch(self, tx: Any, collection_id: UUID) -> int | None:
        """Atomic increment (C-I6)."""
        epoch = await tx.fetchval(
            "UPDATE collections SET purge_epoch = purge_epoch + 1 WHERE id = $1 "
            "RETURNING purge_epoch",
            collection_id,
        )
        return None if epoch is None else int(epoch)

    async def close_tender(self, tx: Any, tenant_id: UUID, tender_id: UUID, at: datetime) -> int:
        """``tender_closed_at`` once, on every tender row whatever its status (COL-14)."""
        status = await tx.execute(
            "UPDATE collections SET tender_closed_at = $3 "
            "WHERE tenant_id = $1 AND tender_id = $2 AND type = 'tender' "
            "AND tender_closed_at IS NULL",
            tenant_id,
            tender_id,
            at,
        )
        return _count(status)

    async def delete_tenant_rows(self, tx: Any, tenant_id: UUID) -> int:
        """Dependants first, promotion targets before their sources (``fk_coll_promoted_from``)."""
        for table in ("retention_policies", "collection_grants", "index_generations"):
            await tx.execute(f"DELETE FROM {table} WHERE tenant_id = $1", tenant_id)
        targets = await tx.execute(
            "DELETE FROM collections WHERE tenant_id = $1 AND promoted_from IS NOT NULL", tenant_id
        )
        rest = await tx.execute("DELETE FROM collections WHERE tenant_id = $1", tenant_id)
        return _count(targets) + _count(rest)


_GENERATION = (
    "collection_id, generation, tenant_id, embedding_profile, chunker_version, status, "
    "registered_at, verified_at, failed_checks, activated_at, retired_at"
)


def _generation(r: Any) -> IndexGeneration:
    checks = r["failed_checks"]
    return IndexGeneration(
        collection_id=r["collection_id"],
        generation=r["generation"],
        tenant_id=r["tenant_id"],
        embedding_profile=r["embedding_profile"],
        chunker_version=r["chunker_version"],
        status=IndexGenerationStatus(r["status"]),
        registered_at=r["registered_at"],
        verified_at=r["verified_at"],
        failed_checks=None if checks is None else json.loads(checks),
        activated_at=r["activated_at"],
        retired_at=r["retired_at"],
    )


class PgIndexGenerationRepository:
    """``index_generations``."""

    async def get(self, tx: Any, collection_id: UUID, generation: int) -> IndexGeneration | None:
        """One generation."""
        row = await tx.fetchrow(
            f"SELECT {_GENERATION} FROM index_generations "
            "WHERE collection_id = $1 AND generation = $2",
            collection_id,
            generation,
        )
        return None if row is None else _generation(row)

    async def get_active(self, tx: Any, collection_id: UUID) -> IndexGeneration | None:
        """The one active generation (``uq_gen_active``)."""
        row = await tx.fetchrow(
            f"SELECT {_GENERATION} FROM index_generations "
            "WHERE collection_id = $1 AND status = 'active'",
            collection_id,
        )
        return None if row is None else _generation(row)

    async def latest_retired_before(
        self, tx: Any, collection_id: UUID, generation: int
    ) -> int | None:
        """The highest retired generation number below ``generation``."""
        value = await tx.fetchval(
            "SELECT max(generation) FROM index_generations "
            "WHERE collection_id = $1 AND status = 'retired' AND generation < $2",
            collection_id,
            generation,
        )
        return None if value is None else int(value)

    async def insert(self, tx: Any, generation: IndexGeneration) -> None:
        """Insert (trigger: ``registered``, ``verified`` or ``active`` only)."""
        g = generation
        await tx.execute(
            "INSERT INTO index_generations (collection_id, generation, tenant_id, "
            "embedding_profile, chunker_version, status, registered_at, verified_at, "
            "failed_checks, activated_at, retired_at) "
            "VALUES ($1, $2, $3, $4, $5, $6, COALESCE($7, now()), $8, $9::jsonb, $10, $11)",
            g.collection_id,
            g.generation,
            g.tenant_id,
            g.embedding_profile,
            g.chunker_version,
            g.status.value,
            g.registered_at,
            g.verified_at,
            None if g.failed_checks is None else json.dumps(g.failed_checks),
            g.activated_at,
            g.retired_at,
        )

    async def update(self, tx: Any, generation: IndexGeneration) -> None:
        """Write the status and its timestamps; the trigger refuses any illegal edge (IG-5)."""
        g = generation
        await tx.execute(
            "UPDATE index_generations SET status = $3, verified_at = $4, "
            "failed_checks = $5::jsonb, activated_at = $6, retired_at = $7 "
            "WHERE collection_id = $1 AND generation = $2",
            g.collection_id,
            g.generation,
            g.status.value,
            g.verified_at,
            None if g.failed_checks is None else json.dumps(g.failed_checks),
            g.activated_at,
            g.retired_at,
        )


class PgGrantRepository:
    """``collection_grants`` (read only: this service mints no grant, CG-2)."""

    async def list_active(
        self, tx: Any, collection_id: UUID, *, limit: int, offset: int
    ) -> list[CollectionGrant]:
        """Live projection rows, oldest first."""
        rows = await tx.fetch(
            "SELECT id, collection_id, tenant_id, subject_type, subject_id, level, projected_at, "
            "source, expires_at FROM collection_grants "
            "WHERE collection_id = $1 AND deleted_at IS NULL "
            "ORDER BY granted_at, id LIMIT $2 OFFSET $3",
            collection_id,
            limit,
            offset,
        )
        return [
            CollectionGrant(
                id=r["id"],
                collection_id=r["collection_id"],
                tenant_id=r["tenant_id"],
                subject_type=GrantSubjectType(r["subject_type"]),
                subject_id=r["subject_id"],
                level=GrantLevel(r["level"]),
                projected_at=r["projected_at"],
                source=r["source"],
                expires_at=r["expires_at"],
            )
            for r in rows
        ]


_RETENTION = (
    "collection_id, tenant_id, retain_days, legal_hold, updated_by, record_version, updated_at"
)


def _policy(r: Any) -> RetentionPolicy:
    return RetentionPolicy(
        collection_id=r["collection_id"],
        tenant_id=r["tenant_id"],
        retain_days=r["retain_days"],
        legal_hold=r["legal_hold"],
        updated_by=r["updated_by"],
        record_version=r["record_version"],
        updated_at=r["updated_at"],
    )


class PgRetentionRepository:
    """``retention_policies``."""

    async def get(
        self, tx: Any, collection_id: UUID, *, lock: bool = False
    ) -> RetentionPolicy | None:
        """The policy, if a P-6 write created one."""
        row = await tx.fetchrow(
            f"SELECT {_RETENTION} FROM retention_policies WHERE collection_id = $1"
            f"{_LOCK if lock else ''}",
            collection_id,
        )
        return None if row is None else _policy(row)

    async def insert(self, tx: Any, policy: RetentionPolicy) -> RetentionPolicy | None:
        """Create; ``None`` where a concurrent first write won."""
        row = await tx.fetchrow(
            "INSERT INTO retention_policies (collection_id, tenant_id, retain_days, legal_hold, "
            "updated_by) VALUES ($1, $2, $3, $4, $5) ON CONFLICT (collection_id) DO NOTHING "
            f"RETURNING {_RETENTION}",
            policy.collection_id,
            policy.tenant_id,
            policy.retain_days,
            policy.legal_hold,
            policy.updated_by,
        )
        return None if row is None else _policy(row)

    async def update(
        self, tx: Any, policy: RetentionPolicy, expected_version: int
    ) -> RetentionPolicy | None:
        """Update guarded by ``record_version`` (§14.1)."""
        row = await tx.fetchrow(
            "UPDATE retention_policies SET retain_days = $3, legal_hold = $4, updated_by = $5 "
            f"WHERE collection_id = $1 AND record_version = $2 RETURNING {_RETENTION}",
            policy.collection_id,
            expected_version,
            policy.retain_days,
            policy.legal_hold,
            policy.updated_by,
        )
        return None if row is None else _policy(row)


class PgTenantLifecycleRepository:
    """``tenant_lifecycle``."""

    async def get(self, tx: Any, tenant_id: UUID, *, lock: bool = False) -> TenantLifecycle | None:
        """The frozen or purged row, if any."""
        row = await tx.fetchrow(
            "SELECT tenant_id, state, cause, changed_at, record_version FROM tenant_lifecycle "
            f"WHERE tenant_id = $1{_LOCK if lock else ''}",
            tenant_id,
        )
        if row is None:
            return None
        return TenantLifecycle(
            tenant_id=row["tenant_id"],
            state=TenantState(row["state"]),
            changed_at=row["changed_at"],
            cause=row["cause"],
            record_version=row["record_version"],
        )

    async def upsert(self, tx: Any, row: TenantLifecycle) -> None:
        """Insert, or update state, cause and ``changed_at`` (a purged row refuses, TL-1)."""
        await tx.execute(
            "INSERT INTO tenant_lifecycle (tenant_id, state, cause, changed_at) "
            "VALUES ($1, $2, $3, $4) ON CONFLICT (tenant_id) DO UPDATE SET "
            "state = EXCLUDED.state, cause = EXCLUDED.cause, changed_at = EXCLUDED.changed_at",
            row.tenant_id,
            row.state.value,
            row.cause,
            row.changed_at,
        )

    async def delete(self, tx: Any, tenant_id: UUID) -> None:
        """Remove a ``frozen`` row on reactivation (TL-2)."""
        await tx.execute("DELETE FROM tenant_lifecycle WHERE tenant_id = $1", tenant_id)
