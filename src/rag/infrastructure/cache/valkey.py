"""Valkey-backed string cache (§11). Every failure is a miss: correctness never depends on it."""

from __future__ import annotations

import time
from collections.abc import Callable

import redis.asyncio as redis
from fastapicommon.logging import get_logger

from rag.infrastructure.config import Settings
from rag.infrastructure.observability.metrics import cache_failure_hook

log = get_logger("rag.cache")
LOG_EVERY_SECONDS = 60.0


class ValkeyCache:
    """Satisfies ``corpusscope.ScopeCache`` and the application's ``KeyValueCache``.

    The connection is lazy (no I/O at construction). Keys are tenant-prefixed by the callers
    (§4.10); a Valkey outage bypasses to the source (§11.4).
    """

    def __init__(self, url: str, on_failure: Callable[[str], None] | None = None) -> None:
        self._client: redis.Redis = redis.from_url(url, decode_responses=True)
        self._on_failure = on_failure
        self._down = False
        self._last_logged = float("-inf")
        self._logged = False

    def _failed(self, op: str, exc: Exception) -> None:
        """Count every failure; log the first after a healthy spell (an outage is never silent)."""
        if self._on_failure:
            self._on_failure(op)
        if self._down:
            return
        self._down = True
        now = time.monotonic()
        if now - self._last_logged >= LOG_EVERY_SECONDS:  # a flapping Valkey must not flood
            self._last_logged = now
            self._logged = True
            log.warning("cache_unavailable", op=op, error_type=type(exc).__name__)
        else:
            self._logged = False

    def _ok(self) -> None:
        if self._down:
            self._down = False
            if self._logged:  # a recovery line only for an outage that was announced
                log.info("cache_recovered")

    async def get(self, key: str) -> str | None:
        """The value, or ``None`` on a miss or any error."""
        try:
            value = await self._client.get(key)
        except Exception as exc:
            self._failed("get", exc)
            return None
        self._ok()
        return value if isinstance(value, str) else None

    async def set(self, key: str, value: str, ttl_seconds: int) -> None:
        """Best-effort store with a TTL."""
        try:
            await self._client.set(key, value, ex=ttl_seconds)
        except Exception as exc:
            self._failed("set", exc)
        else:
            self._ok()

    async def aclose(self) -> None:
        """Close the connection pool."""
        try:
            await self._client.aclose()
        except Exception:  # noqa: S110
            pass


def build_cache(settings: Settings) -> ValkeyCache | None:
    """``None`` where no Valkey URL is configured (every cache then bypasses to source)."""
    if not settings.valkey_url:
        return None
    return ValkeyCache(settings.valkey_url, cache_failure_hook(settings))
