"""Repository ports of the corpus module. ``tx`` is the opaque transaction-bound connection."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Any, Protocol
from uuid import UUID

from rag.domain.corpus.collection import Collection
from rag.domain.corpus.grant import CollectionGrant
from rag.domain.corpus.index_generation import IndexGeneration
from rag.domain.corpus.retention import RetentionPolicy
from rag.domain.corpus.tenant_lifecycle import TenantLifecycle
from rag.domain.shared.enums import CollectionType

type Tx = Any


class CollectionRepository(Protocol):
    """``collections``. Lookups taking ``lock`` issue ``FOR UPDATE``."""

    async def get(self, tx: Tx, collection_id: UUID, *, lock: bool = False) -> Collection | None:
        """The row visible under RLS (the PLATFORM reference row included), or ``None``."""
        ...

    async def find_by_tender(
        self, tx: Tx, tenant_id: UUID, tender_id: UUID, type_: CollectionType, *, lock: bool = False
    ) -> list[Collection]:
        """Every non-superseded row of ``(tenant, tender, type)``, whatever ``restricted``."""
        ...

    async def list_by_tenant(self, tx: Tx, tenant_id: UUID) -> list[Collection]:
        """Every row of the tenant itself (never the PLATFORM sentinel's), locked."""
        ...

    async def list_by_ids(
        self, tx: Tx, ids: Sequence[UUID], *, limit: int, offset: int
    ) -> list[Collection]:
        """The named rows, ordered by ``(created_at, id)`` for stable paging."""
        ...

    async def insert(self, tx: Tx, collection: Collection) -> bool:
        """``INSERT ... ON CONFLICT (id) DO NOTHING``; ``False`` if the id exists (COL-11)."""
        ...

    async def update(self, tx: Tx, collection: Collection, expected_version: int) -> int | None:
        """Write the mutable columns ``WHERE record_version = expected``; version or ``None``."""
        ...

    async def bump_purge_epoch(self, tx: Tx, collection_id: UUID) -> int | None:
        """Atomic ``purge_epoch + 1`` (C-I6); the new epoch, or ``None`` when absent."""
        ...

    async def close_tender(self, tx: Tx, tenant_id: UUID, tender_id: UUID, at: datetime) -> int:
        """Set ``tender_closed_at`` ``WHERE IS NULL`` on every tender row (COL-14); rows changed."""
        ...

    async def list_stalled_promotions(self, tx: Tx, before: datetime) -> list[Collection]:
        """Promotion targets still ``provisioning`` that were created before ``before`` (§21.2)."""
        ...

    async def delete_tenant_rows(self, tx: Tx, tenant_id: UUID) -> int:
        """Phase two of a tenant purge: delete the tenant's own rows and dependants (§21.1)."""
        ...


class IndexGenerationRepository(Protocol):
    """``index_generations``."""

    async def get(self, tx: Tx, collection_id: UUID, generation: int) -> IndexGeneration | None:
        """One generation, or ``None``."""
        ...

    async def get_active(self, tx: Tx, collection_id: UUID) -> IndexGeneration | None:
        """The ``active`` generation, or ``None``."""
        ...

    async def latest_retired_before(
        self, tx: Tx, collection_id: UUID, generation: int
    ) -> int | None:
        """The highest retired generation below ``generation`` (a repeat C-I4's ``previous``)."""
        ...

    async def insert(self, tx: Tx, generation: IndexGeneration) -> None:
        """Insert (IG-3 admits ``registered``, ``verified`` and ``active``)."""
        ...

    async def update(self, tx: Tx, generation: IndexGeneration) -> None:
        """Write status and timestamps."""
        ...


class GrantRepository(Protocol):
    """``collection_grants`` (read side only: this service mints nothing, CG-2)."""

    async def list_active(
        self, tx: Tx, collection_id: UUID, *, limit: int, offset: int
    ) -> list[CollectionGrant]:
        """Rows with ``deleted_at IS NULL``, oldest first."""
        ...


class RetentionRepository(Protocol):
    """``retention_policies``."""

    async def get(
        self, tx: Tx, collection_id: UUID, *, lock: bool = False
    ) -> RetentionPolicy | None:
        """The policy, or ``None`` before the first P-6 write."""
        ...

    async def insert(self, tx: Tx, policy: RetentionPolicy) -> RetentionPolicy | None:
        """Create (ON CONFLICT DO NOTHING); the stored row, or ``None`` when one already exists."""
        ...

    async def update(
        self, tx: Tx, policy: RetentionPolicy, expected_version: int
    ) -> RetentionPolicy | None:
        """Update ``WHERE record_version = expected``; the stored row, or ``None`` when stale."""
        ...


class TenantLifecycleRepository(Protocol):
    """``tenant_lifecycle``."""

    async def get(self, tx: Tx, tenant_id: UUID, *, lock: bool = False) -> TenantLifecycle | None:
        """The row, or ``None`` when the tenant is served."""
        ...

    async def upsert(self, tx: Tx, row: TenantLifecycle) -> None:
        """Insert or update state, cause and ``changed_at``."""
        ...

    async def delete(self, tx: Tx, tenant_id: UUID) -> None:
        """Delete a ``frozen`` row on reactivation (TL-2)."""
        ...
