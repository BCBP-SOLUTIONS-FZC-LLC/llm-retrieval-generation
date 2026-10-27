"""Scope resolution (§6.2, §13.2): who may read which collections, at which level."""

from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import TypeVar
from uuid import UUID

from rag.corpusscope.errors import (
    ScopeError,
    ScopeLevelInsufficient,
    ScopeNotFound,
    ScopeStoreUnavailable,
)
from rag.corpusscope.model import (
    PLATFORM,
    STATUS_ACTIVE,
    TYPE_REFERENCE,
    TYPE_TENANT,
    TYPE_TENDER,
    CollectionRow,
    Level,
    Principal,
    ScopeRequest,
)
from rag.corpusscope.ports import CollectionStore, ScopeCache, TenderAccess
from rag.corpusscope.token import ScopedCollection, ScopeToken

T = TypeVar("T")

GENERATING_ROLES = frozenset({"tenant_owner", "tenant_admin", "tender_admin", "member"})
"""§6.2 role table: IAM's four roles all resolve to ``generate``; no role means ``search``."""

TAC4_LEVELS = {"view": Level.ANSWER, "edit": Level.GENERATE, "approve": Level.GENERATE}
"""§4.7.2 (LD-21). An unknown label confers nothing (fail closed)."""

DEFAULT_WEIGHTS = {
    TYPE_TENDER: Decimal("1.00"),
    TYPE_TENANT: Decimal("0.90"),
    TYPE_REFERENCE: Decimal("0.80"),
}
"""Assumption: HLD §7.3's per-type defaults come from the Domain Profile, which this vendored
library does not see. ``weight_override`` on the row wins."""


def _digest(*parts: str) -> str:
    return hashlib.sha256("|".join(parts).encode()).hexdigest()[:16]


def role_level(principal: Principal) -> Level:
    """The level a tenant role confers (§6.2): ``generate`` for any IAM role, else ``search``."""
    return Level.GENERATE if principal.roles & GENERATING_ROLES else Level.SEARCH


@dataclass(slots=True)
class ScopeResolver:
    """Resolves a ``ScopeToken``. Fails closed everywhere: no default scope, no breaker (§14.3).

    ``store_timeout_seconds`` bounds each primary read and TAC-4 call (100 ms, one retry).
    ``cache`` holds whole resolutions for ``cache_ttl_seconds`` (SCOPE-4); a cache error is a
    miss. ``clock`` is injectable for tests.
    """

    store: CollectionStore
    tac4: TenderAccess
    cache: ScopeCache | None = None
    token_ttl_seconds: int = 600
    cache_ttl_seconds: int = 60
    store_timeout_seconds: float = 0.1
    weights: dict[str, Decimal] = field(default_factory=lambda: dict(DEFAULT_WEIGHTS))
    clock: Callable[[], datetime] = lambda: datetime.now(UTC)

    async def resolve(
        self, principal: Principal, request: ScopeRequest, *, required: Level | None = None
    ) -> ScopeToken:
        """The token for ``request``; ``required`` is the operation's level (§5.3).

        Raises ``ScopeNotFound``, ``ScopeLevelInsufficient`` or ``ScopeStoreUnavailable``.
        """
        now = self.clock()
        token = await self._cached(principal, request, now)
        if token is None:
            token = await self._fresh(principal, request, now)
            await self._store_cache(principal, request, token)
        if required is not None and token.level < required:
            raise ScopeLevelInsufficient(f"requires {required.label}, holds {token.level.label}")
        return token

    # --- cache -----------------------------------------------------------------------------

    def _key(self, principal: Principal, request: ScopeRequest) -> str:
        who = "-" if principal.principal_id is None else str(principal.principal_id)
        # The LLD key carries a department hash only; roles feed the level, so they join it.
        standing = _digest(
            ",".join(sorted(map(str, principal.departments))), ",".join(sorted(principal.roles))
        )
        asked = _digest(
            json.dumps(
                [
                    None if request.tender_id is None else str(request.tender_id),
                    sorted(map(str, request.collection_ids)),
                    sorted(request.collection_types),
                ]
            )
        )
        return f"{principal.tenant_id}:scope:{who}:{standing}:{asked}"

    async def _cached(
        self, principal: Principal, request: ScopeRequest, now: datetime
    ) -> ScopeToken | None:
        if self.cache is None:
            return None
        try:
            raw = await self.cache.get(self._key(principal, request))
            if raw is None:
                return None
            token = ScopeToken.from_json(raw)
        except Exception:  # a failing or corrupt cache is a miss (§11.4)
            return None
        fresh = now - token.resolved_at < timedelta(seconds=self.cache_ttl_seconds)
        mine = (
            token.tenant_id == principal.tenant_id and token.principal_id == principal.principal_id
        )
        return token if fresh and mine and now < token.expires_at else None

    async def _store_cache(
        self, principal: Principal, request: ScopeRequest, token: ScopeToken
    ) -> None:
        if self.cache is None:
            return
        try:
            await self.cache.set(
                self._key(principal, request), token.to_json(), self.cache_ttl_seconds
            )
        except Exception:  # noqa: S110 - cache write is best effort
            pass

    # --- resolution ------------------------------------------------------------------------

    async def _read(self, call: Callable[[], Awaitable[T]]) -> T:
        """100 ms, one retry, then ``ScopeStoreUnavailable`` (§13.2, §14.3). Never a default."""
        last: Exception | None = None
        for _ in range(2):
            try:
                return await asyncio.wait_for(call(), self.store_timeout_seconds)
            except ScopeError:
                raise
            except Exception as exc:
                last = exc
        raise ScopeStoreUnavailable("grant state could not be read") from last

    async def _fresh(
        self, principal: Principal, request: ScopeRequest, now: datetime
    ) -> ScopeToken:
        rows = await self._read(lambda: self.store.load(principal.tenant_id))
        # Defence in depth beside RLS: only this tenant's rows and the PLATFORM reference rows.
        rows = [
            r
            for r in rows
            if r.tenant_id == principal.tenant_id
            or (r.type == TYPE_REFERENCE and r.tenant_id == PLATFORM)
        ]
        named = set(request.collection_ids)
        candidates = [r for r in rows if r.status == STATUS_ACTIVE]  # COL-10, SCOPE-11
        if request.collection_types:
            candidates = [r for r in candidates if r.type in request.collection_types]
        if request.tender_id is not None:
            candidates = [
                r for r in candidates if r.type != TYPE_TENDER or r.tender_id == request.tender_id
            ]
        if named:
            candidates = [r for r in candidates if r.id in named]

        tac_cache: dict[UUID, Level | None] = {}
        granted: list[tuple[CollectionRow, Level]] = []
        for row in candidates:
            level = await self._standing(principal, request, row, tac_cache)
            if level is not None:
                granted.append((row, level))

        got = {r.id for r, _ in granted}
        # A named collection or tender that resolves nothing is a 404, never a silent narrowing.
        if named - got:
            raise ScopeNotFound("a named collection is not available")
        if request.tender_id is not None and not any(
            r.type == TYPE_TENDER and r.tender_id == request.tender_id for r, _ in granted
        ):
            raise ScopeNotFound("the named tender has no collection in scope")
        if not granted:
            raise ScopeNotFound("nothing is in scope")  # SCOPE-6: no fallback, no widening

        scoped = tuple(self._scoped(r, lvl) for r, lvl in granted)
        # Reference collections confer no standing (§6.2); counting them in the clamp would
        # make every unnamed request `search`. Deviation recorded in the plan.
        standing = [c.level for c in scoped if c.type != TYPE_REFERENCE] or [Level.SEARCH]
        return ScopeToken.create(
            tenant_id=principal.tenant_id,
            principal_id=principal.principal_id,
            collections=scoped,
            level=min(standing),
            clamped=len(set(standing)) > 1,
            resolved_at=now,
            expires_at=now + timedelta(seconds=self.token_ttl_seconds),
        )

    async def _standing(
        self,
        principal: Principal,
        request: ScopeRequest,
        row: CollectionRow,
        tac_cache: dict[UUID, Level | None],
    ) -> Level | None:
        if row.type == TYPE_REFERENCE:
            return Level.SEARCH
        if row.type == TYPE_TENANT:
            return role_level(principal)
        if not row.is_restricted:
            mine = set(principal.departments) & set(row.owning_departments)
            return role_level(principal) if mine else None  # CG-1: departments, never a row
        # Restricted: only where the request names the tender or the collection (§4.7.2).
        asked = request.tender_id == row.tender_id or row.id in request.collection_ids
        if not asked or principal.principal_id is None:
            return None
        if row.tender_id not in tac_cache:
            who = principal.principal_id
            label = await self._read(
                lambda: self.tac4.level(principal.tenant_id, row.tender_id, who)
            )
            tac_cache[row.tender_id] = None if label is None else TAC4_LEVELS.get(label)
        return tac_cache[row.tender_id]

    def _scoped(self, row: CollectionRow, level: Level) -> ScopedCollection:
        weight = (
            Decimal(str(row.weight_override)).quantize(Decimal("0.01"))
            if row.weight_override is not None
            else self.weights[row.type]
        )
        return ScopedCollection(
            id=row.id,
            type=row.type,
            level=level,
            weight=weight,
            physical_collection=row.physical_collection,
            shard_key=row.shard_key,
            tenant_id=row.tenant_id,
            tender_id=row.tender_id,
            index_generation=row.active_generation,
            purge_epoch=row.purge_epoch,
        )
