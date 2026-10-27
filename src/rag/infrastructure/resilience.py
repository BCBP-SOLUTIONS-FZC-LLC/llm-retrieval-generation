"""A small circuit breaker (§14.3): opens on consecutive failures, half-open after a pause."""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable
from typing import TypeVar

from fastapicommon.logging import get_logger

T = TypeVar("T")
log = get_logger("rag.resilience")


class BreakerOpen(Exception):  # noqa: N818
    """The breaker is open: the call was not attempted."""


class CircuitBreaker:
    """5 consecutive failures open it; after 30 s one probe is let through (half-open).

    One instance per dependency, shared by every call that §14.3 puts behind the same breaker
    (the Qdrant search and the entity count share one).
    """

    def __init__(
        self,
        failure_threshold: int = 5,
        reset_seconds: float = 30.0,
        clock: Callable[[], float] = time.monotonic,
        name: str = "breaker",
    ) -> None:
        self._threshold, self._reset, self._clock = failure_threshold, reset_seconds, clock
        self._name = name
        self._failures = 0
        self._opened_at: float | None = None

    @property
    def is_open(self) -> bool:
        """Whether calls are currently refused."""
        return self._opened_at is not None and self._clock() - self._opened_at < self._reset

    async def call(self, fn: Callable[[], Awaitable[T]]) -> T:
        """Run ``fn``; count its failures. Raises ``BreakerOpen`` while open."""
        if self.is_open:
            raise BreakerOpen
        try:
            result = await fn()
        except Exception:
            self._failures += 1
            if self._failures >= self._threshold or self._opened_at is not None:
                if not self.is_open:
                    log.warning("circuit_opened", breaker=self._name, failures=self._failures)
                self._opened_at = self._clock()  # (re)open; a failed probe restarts the pause
            raise
        self._failures, self._opened_at = 0, None
        return result
