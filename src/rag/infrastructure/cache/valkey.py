"""Valkey-backed string cache (§11). Every failure is a miss: correctness never depends on it."""

from __future__ import annotations

import redis.asyncio as redis

from rag.infrastructure.config import Settings


class ValkeyCache:
    """Satisfies ``corpusscope.ScopeCache`` and the application's ``KeyValueCache``.

    The connection is lazy (no I/O at construction). Keys are tenant-prefixed by the callers
    (§4.10); a Valkey outage bypasses to the source (§11.4).
    """

    def __init__(self, url: str) -> None:
        self._client: redis.Redis = redis.from_url(url, decode_responses=True)

    async def get(self, key: str) -> str | None:
        """The value, or ``None`` on a miss or any error."""
        try:
            value = await self._client.get(key)
        except Exception:
            return None
        return value if isinstance(value, str) else None

    async def set(self, key: str, value: str, ttl_seconds: int) -> None:
        """Best-effort store with a TTL."""
        try:
            await self._client.set(key, value, ex=ttl_seconds)
        except Exception:  # noqa: S110
            pass

    async def aclose(self) -> None:
        """Close the connection pool."""
        try:
            await self._client.aclose()
        except Exception:  # noqa: S110
            pass


def build_cache(settings: Settings) -> ValkeyCache | None:
    """``None`` where no Valkey URL is configured (every cache then bypasses to source)."""
    return ValkeyCache(settings.valkey_url) if settings.valkey_url else None
