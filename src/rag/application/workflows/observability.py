"""§16.2 async-run and regeneration instrumentation as a port.

Labels are bounded vocabularies (outcomes, signal names, scopes, origins, resolutions). Never a
tenant, user, run id or any content (OBS-1, OBS-2).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from typing import Protocol


class WorkflowObserver(Protocol):
    """Metrics, logs and spans of W-1 to W-8 and A-5 to A-7."""

    def span(self, name: str) -> AbstractAsyncContextManager[None]:
        """A trace span around one stage (§16.3). Attributes carry counts, never text."""
        ...

    def workflow_start(self, outcome: str) -> None:
        """``started``, ``already_started`` or ``failed`` (the run stays ``pending``, ASYNC-2)."""
        ...

    def workflow_signal(self, signal: str, outcome: str) -> None:
        """A signal after commit: ``sent`` or ``failed``."""
        ...

    def gate_action(self, action: str) -> None:
        """``approve``, ``revise``, ``cancel``, ``resume`` or ``rematerialise`` accepted."""
        ...

    def scope_revoked(self) -> None:
        """A run ended ``scope_revoked`` by a signal route's re-resolution (§16.2)."""
        ...

    def regeneration(self, scope: str, origin: str) -> None:
        """A regeneration admitted: ``section`` or ``document`` (§16.2)."""
        ...

    def fact_conflict(self, resolution: str) -> None:
        """A fact conflict resolved (A-7)."""
        ...


class NullWorkflowObserver:
    """The observer when metrics are off."""

    @asynccontextmanager
    async def span(self, name: str) -> AsyncIterator[None]:
        """No span."""
        yield

    def workflow_start(self, outcome: str) -> None:
        """No-op."""

    def workflow_signal(self, signal: str, outcome: str) -> None:
        """No-op."""

    def gate_action(self, action: str) -> None:
        """No-op."""

    def scope_revoked(self) -> None:
        """No-op."""

    def regeneration(self, scope: str, origin: str) -> None:
        """No-op."""

    def fact_conflict(self, resolution: str) -> None:
        """No-op."""
