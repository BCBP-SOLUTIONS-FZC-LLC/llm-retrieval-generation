"""Time source."""

from __future__ import annotations

from datetime import UTC, datetime


class SystemClock:
    """Wall-clock UTC."""

    def now(self) -> datetime:
        """Current timezone-aware UTC time."""
        return datetime.now(UTC)
