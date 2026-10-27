"""List-window rules of LLD 5.1: default 100, ceiling 500 (clamped), echoed on every list."""

from __future__ import annotations

from dataclasses import dataclass

DEFAULT_LIMIT = 100
MAX_LIMIT = 500


@dataclass(frozen=True, slots=True)
class Window:
    """The applied ``limit`` / ``offset``."""

    limit: int = DEFAULT_LIMIT
    offset: int = 0


def clamp_window(limit: int | None = None, offset: int | None = None) -> Window:
    """Apply the defaults and clamp ``limit`` to the ceiling instead of rejecting it.

    A non-positive ``limit`` or negative ``offset`` is a ``400 VALIDATION_FAILED`` that the
    presentation layer answers before reaching here; it raises ``ValueError`` as a backstop.
    """
    if limit is not None and limit < 1:
        raise ValueError("limit must be positive")
    if offset is not None and offset < 0:
        raise ValueError("offset must not be negative")
    return Window(min(limit if limit is not None else DEFAULT_LIMIT, MAX_LIMIT), offset or 0)
