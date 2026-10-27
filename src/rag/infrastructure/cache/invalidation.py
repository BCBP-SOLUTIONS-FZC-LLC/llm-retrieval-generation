"""Valkey eviction for the event consumers (§11.1): tenant-prefixed keys, SCAN never KEYS."""

from __future__ import annotations

from collections.abc import Awaitable
from typing import Any, Protocol
from uuid import UUID


class _Valkey(Protocol):
    def scan_iter(self, match: str, count: int = ...) -> Any: ...

    def delete(self, *keys: str) -> Awaitable[Any]: ...


class ValkeyProfileCache:
    """``ProfileCache``: delete ``{tenant}:pack:{domain}:{language}:{concern}:*`` entries.

    ASSUMPTION: §11.1 gives the key as ``{tenant}:pack:{identity}:{version}`` without fixing the
    identity's shape; it is taken as ``{domain}:{language}:{concern}`` here, a one-line change in
    ``pattern`` if the retrieval slice settles it differently. The tenant prefix is what isolates.
    The dropped version is the last key segment, so ``from_version`` is a best guess.
    """

    def __init__(self, client: _Valkey) -> None:
        self._c = client

    @staticmethod
    def pattern(tenant_id: UUID, domain: str, language: str, concern: str) -> str:
        """The SCAN pattern of one tenant's entries for one profile."""
        return f"{tenant_id}:pack:{domain}:{language}:{concern}:*"

    async def invalidate(
        self, tenant_id: UUID, domain: str, language: str, concern: str
    ) -> str | None:
        """Drop the matching entries; the version of the last one dropped, if any."""
        version: str | None = None
        keys: list[str] = []
        async for key in self._c.scan_iter(
            match=self.pattern(tenant_id, domain, language, concern), count=200
        ):
            text = key.decode() if isinstance(key, bytes) else str(key)
            keys.append(text)
            version = text.rsplit(":", 1)[-1]
        if keys:
            await self._c.delete(*keys)
        return version


class ValkeyPlacementCache:
    """``PlacementCache``: ``DEL {tenant}:coll:{id}`` on a purge-epoch bump (§11.1)."""

    def __init__(self, client: _Valkey) -> None:
        self._c = client

    async def evict(self, tenant_id: UUID, collection_id: UUID) -> None:
        """Delete the one key."""
        await self._c.delete(f"{tenant_id}:coll:{collection_id}")
