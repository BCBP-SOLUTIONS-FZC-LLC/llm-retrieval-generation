"""Fakes and builders for corpusscope tests."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from rag.corpusscope import (
    PLATFORM,
    REFERENCE,
    TENANT_WIDE,
    CollectionRow,
    Principal,
    ScopeResolver,
)

TENANT = UUID("0a3f0000-0000-7000-8000-000000000001")
OTHER_TENANT = UUID("0a3f0000-0000-7000-8000-0000000000ff")
USER = UUID("0a3f0000-0000-7000-8000-000000000002")
DEPT_A = UUID("3fa85f64-5717-4562-b3fc-2c963f66afa6")
DEPT_B = UUID("3fa85f64-5717-4562-b3fc-2c963f66afb7")
T0 = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)


def principal(
    *roles: str, departments: tuple[UUID, ...] = (DEPT_A,), tenant: UUID = TENANT
) -> Principal:
    """A tenant member in ``departments`` holding ``roles``."""
    return Principal(tenant, USER, frozenset(roles), departments)


def row(
    type_: str = "tender",
    *,
    tender: UUID | None = None,
    status: str = "active",
    restricted: bool = False,
    announced: bool = False,
    generation: int | None = 1,
    departments: tuple[UUID, ...] = (DEPT_A,),
    tenant: UUID | None = None,
    epoch: int = 0,
    weight: float | None = None,
) -> CollectionRow:
    """A ``collections`` row consistent with §4.10's sentinel rules."""
    if type_ == "reference":
        tenant_id, tender_id = PLATFORM, REFERENCE
    elif type_ == "tenant":
        tenant_id, tender_id = tenant or TENANT, TENANT_WIDE
    else:
        tenant_id, tender_id = tenant or TENANT, tender or uuid4()
    return CollectionRow(
        id=uuid4(),
        tenant_id=tenant_id,
        type=type_,
        tender_id=tender_id,
        restricted=restricted,
        status=status,
        physical_collection=f"phys_{type_}",
        shard_key=f"shard_{tender_id.hex[:6]}",
        purge_epoch=epoch,
        active_generation=generation,
        weight_override=weight,
        owning_departments=() if restricted or announced else departments,
        restricted_announced_at=T0 if announced else None,
    )


class FakeStore:
    """Returns ``rows``; ``fail`` raises that many times first; counts calls."""

    def __init__(self, rows: list[CollectionRow]) -> None:
        self.rows, self.calls, self.fail = rows, 0, 0

    async def load(self, tenant_id: UUID) -> list[CollectionRow]:
        self.calls += 1
        if self.fail:
            self.fail -= 1
            raise ConnectionError("primary down")
        return list(self.rows)


class FakeTac4:
    """TAC-4 answers per tender; ``error`` makes it unreachable."""

    def __init__(self, answers: dict[UUID, str | None] | None = None) -> None:
        self.answers, self.calls, self.error = answers or {}, 0, False

    async def level(self, tenant_id: UUID, tender_id: UUID, principal_id: UUID) -> str | None:
        self.calls += 1
        if self.error:
            raise TimeoutError("TAC-4 unreachable")
        return self.answers.get(tender_id)


class DictCache:
    """In-memory ``ScopeCache``; ``broken`` makes every call raise."""

    def __init__(self) -> None:
        self.data: dict[str, str] = {}
        self.broken = False

    async def get(self, key: str) -> str | None:
        if self.broken:
            raise ConnectionError("valkey down")
        return self.data.get(key)

    async def set(self, key: str, value: str, ttl_seconds: int) -> None:
        if self.broken:
            raise ConnectionError("valkey down")
        self.data[key] = value


class Clock:
    """A movable clock."""

    def __init__(self) -> None:
        self.now = T0

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)


def resolver(
    rows: list[CollectionRow],
    tac4: FakeTac4 | None = None,
    cache: DictCache | None = None,
    clock: Clock | None = None,
) -> tuple[ScopeResolver, FakeStore]:
    """A resolver over fakes, and its store."""
    store = FakeStore(rows)
    r = ScopeResolver(
        store=store,
        tac4=tac4 or FakeTac4(),
        cache=cache,
        clock=clock or Clock(),
        store_timeout_seconds=0.5,
    )
    return r, store
