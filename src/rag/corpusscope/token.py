"""``ScopeToken`` (§6.3): the only input to filter construction (SCOPE-1)."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

from rag.corpusscope.errors import FilterError, ScopeError, ScopeExpired
from rag.corpusscope.model import (
    PLATFORM,
    REFERENCE,
    TENANT_WIDE,
    TYPE_REFERENCE,
    TYPE_TENANT,
    TYPE_TENDER,
    Level,
)


@dataclass(frozen=True, slots=True)
class ScopedCollection:
    """One collection a request may read, bound to one index generation (SCOPE-5).

    ``tenant_id`` and ``tender_id`` are the values the points carry: the real ids for a
    ``tender`` collection, the §4.10 sentinels otherwise. ``index_generation`` is ``None`` for
    an empty corpus (SCOPE-12): no filter is ever built for it.
    """

    id: UUID
    type: str
    level: Level
    weight: Decimal
    physical_collection: str
    shard_key: str
    tenant_id: UUID
    tender_id: UUID
    index_generation: int | None
    purge_epoch: int

    def __post_init__(self) -> None:
        """SCOPE-2: sentinels are explicit constants; a null or inconsistent pair raises."""
        if self.type == TYPE_REFERENCE:
            ok = self.tenant_id == PLATFORM and self.tender_id == REFERENCE
        elif self.type == TYPE_TENANT:
            ok = self.tenant_id != PLATFORM and self.tender_id == TENANT_WIDE
        elif self.type == TYPE_TENDER:
            ok = self.tenant_id != PLATFORM and self.tender_id not in (TENANT_WIDE, REFERENCE)
        else:
            ok = False
        if not ok or not self.physical_collection or not self.shard_key:
            raise FilterError(f"collection {self.id}: inconsistent tenant/tender identity")

    @property
    def empty(self) -> bool:
        """No activated index generation (SCOPE-12)."""
        return self.index_generation is None


def canonical_hash(
    tenant_id: UUID,
    principal_id: UUID | None,
    collections: tuple[ScopedCollection, ...],
    tender_ids: tuple[UUID, ...],
) -> str:
    """RS-7: ``sha256`` over canonical JSON (sorted keys, no whitespace, UTF-8)."""
    doc = {
        "tenant_id": str(tenant_id),
        "principal_id": None if principal_id is None else str(principal_id),
        "collections": sorted(
            (
                {
                    "id": str(c.id),
                    "level": c.level.label,
                    "index_generation": c.index_generation,
                }
                for c in collections
            ),
            key=lambda d: str(d["id"]),
        ),
        "tender_ids": sorted(str(t) for t in tender_ids),
    }
    raw = json.dumps(doc, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class ScopeToken:
    """The resolved scope of one request. Immutable; never extended (SCOPE-3).

    ``level`` is the grant level applied (the lowest across the collections that carry
    standing; reference collections confer none, see ``resolver``) and ``clamped`` records
    that the collections held different levels (SCOPE-8).
    """

    tenant_id: UUID
    principal_id: UUID | None
    collections: tuple[ScopedCollection, ...]
    tender_ids: tuple[UUID, ...]
    level: Level
    clamped: bool
    resolved_at: datetime
    expires_at: datetime
    resolution_hash: str

    @classmethod
    def create(
        cls,
        *,
        tenant_id: UUID,
        principal_id: UUID | None,
        collections: tuple[ScopedCollection, ...],
        level: Level,
        clamped: bool,
        resolved_at: datetime,
        expires_at: datetime,
    ) -> ScopeToken:
        """Build a token. Raises ``ScopeError`` for an empty or foreign-tenant scope (SCOPE-6)."""
        if not collections:
            raise ScopeError("a scope token needs at least one collection (SCOPE-6)")
        for c in collections:
            if c.type != TYPE_REFERENCE and c.tenant_id != tenant_id:
                raise ScopeError("a collection of another tenant cannot enter the token")
        tender_ids = tuple(sorted({c.tender_id for c in collections if c.type == TYPE_TENDER}))
        return cls(
            tenant_id,
            principal_id,
            collections,
            tender_ids,
            level,
            clamped,
            resolved_at,
            expires_at,
            canonical_hash(tenant_id, principal_id, collections, tender_ids),
        )

    def require_live(self, now: datetime) -> None:
        """SCOPE-3: an expired token builds no filter."""
        if now >= self.expires_at:
            raise ScopeExpired("the scope token has expired; re-resolve")

    def collection(self, collection_id: UUID) -> ScopedCollection:
        """The scoped collection, or ``FilterError`` where the token does not carry it."""
        for c in self.collections:
            if c.id == collection_id:
                return c
        raise FilterError(f"collection {collection_id} is not in the scope token")

    def to_json(self) -> str:
        """Serialise for the scope cache (a whole resolution, SCOPE-4)."""
        doc: dict[str, Any] = {
            "tenant_id": str(self.tenant_id),
            "principal_id": None if self.principal_id is None else str(self.principal_id),
            "level": self.level.label,
            "clamped": self.clamped,
            "resolved_at": self.resolved_at.isoformat(),
            "expires_at": self.expires_at.isoformat(),
            "hash": self.resolution_hash,
            "collections": [
                {
                    "id": str(c.id),
                    "type": c.type,
                    "level": c.level.label,
                    "weight": str(c.weight),
                    "physical": c.physical_collection,
                    "shard": c.shard_key,
                    "tenant_id": str(c.tenant_id),
                    "tender_id": str(c.tender_id),
                    "generation": c.index_generation,
                    "epoch": c.purge_epoch,
                }
                for c in self.collections
            ],
        }
        return json.dumps(doc)

    @classmethod
    def from_json(cls, raw: str) -> ScopeToken:
        """Rebuild and verify the hash. Raises ``ScopeError`` on any mismatch (cache tamper)."""
        try:
            d = json.loads(raw)
            collections = tuple(
                ScopedCollection(
                    UUID(c["id"]),
                    c["type"],
                    Level.parse(c["level"]),
                    Decimal(c["weight"]),
                    c["physical"],
                    c["shard"],
                    UUID(c["tenant_id"]),
                    UUID(c["tender_id"]),
                    c["generation"],
                    int(c["epoch"]),
                )
                for c in d["collections"]
            )
            principal = None if d["principal_id"] is None else UUID(d["principal_id"])
            token = cls.create(
                tenant_id=UUID(d["tenant_id"]),
                principal_id=principal,
                collections=collections,
                level=Level.parse(d["level"]),
                clamped=bool(d["clamped"]),
                resolved_at=datetime.fromisoformat(d["resolved_at"]),
                expires_at=datetime.fromisoformat(d["expires_at"]),
            )
        except (KeyError, ValueError, TypeError) as exc:
            raise ScopeError("unreadable cached scope") from exc
        if token.resolution_hash != d.get("hash"):
            raise ScopeError("cached scope failed its hash check")
        return token
