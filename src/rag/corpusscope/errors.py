"""Failures of scope resolution and filter construction. The service maps them to §24 codes."""

from __future__ import annotations


class ScopeError(Exception):
    """Base class."""


class ScopeNotFound(ScopeError):  # noqa: N818
    """Nothing in scope, or a named collection not granted/active: ``404`` (CG-8, SCOPE-6)."""


class ScopeLevelInsufficient(ScopeError):  # noqa: N818
    """A grant is held below the operation's level: ``403``."""


class ScopeStoreUnavailable(ScopeError):  # noqa: N818
    """Grant state cannot be read: refuse, never default (CG-4)."""


class ScopeExpired(ScopeError):  # noqa: N818
    """An expired token cannot build a filter (SCOPE-3). Callers re-resolve."""


class FilterError(ScopeError):
    """A filter cannot be built safely: bad sentinel, foreign collection, empty corpus."""
