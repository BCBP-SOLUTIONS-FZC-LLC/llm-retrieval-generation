"""Outbound ports of the event consumers (§12.4)."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Protocol
from uuid import UUID

from rag.application.shared.ports import TxHandle

type LedgerTx = Callable[[Callable[[TxHandle], Awaitable[None]]], Awaitable[None]]
"""Run a function exactly once per event, in one ``llm_generation`` transaction that also writes
the ``processed_events`` row (PE-1). A duplicate delivery runs nothing. Implemented over
``eventcommon.inbox.Store.process`` for the message in hand."""


class ProfileCache(Protocol):
    """The pack cache of this service (``{tenant}:pack:...``, §11.1)."""

    async def invalidate(
        self, tenant_id: UUID, domain: str, language: str, concern: str
    ) -> str | None:
        """Drop the entries of one profile for one tenant; the version dropped, if known.

        Idempotent. Never touches a running workflow's pin (EVT-4): that lives in the run row.
        """
        ...


class PlacementCache(Protocol):
    """``{tenant}:coll:{id}`` entries, evicted on any ``purge_epoch`` bump (§11.1)."""

    async def evict(self, tenant_id: UUID, collection_id: UUID) -> None:
        """Best effort: a Valkey outage bypasses every cache anyway (§11.4)."""
        ...


@dataclass(frozen=True, slots=True)
class SuspendedRun:
    """A run suspended on ``quota_exhausted``."""

    run_id: UUID
    artefact_id: UUID
    principal_id: UUID


class RunDirectory(Protocol):
    """Reads ``generation_runs`` for the quota consumer."""

    async def suspended_on_quota(
        self, tx: TxHandle, *, after: UUID | None, limit: int
    ) -> list[SuspendedRun]:
        """The tenant's runs ``suspended`` with ``failure_reason = quota_exhausted``."""
        ...


class RunResumer(Protocol):
    """Re-resolves scope for the run's stored principal and raises the W-7 signal (ASYNC-4).

    A run whose scope is empty ends ``scope_revoked`` (ASYNC-15); the signal is refused on a run
    that is no longer ``suspended``. The workflows slice implements it.
    """

    @property
    def available(self) -> bool:
        """``False`` while no workflow client is wired: nothing may be ledgered as resumed."""
        ...

    async def resume(self, run: SuspendedRun) -> None:
        """Signal one run."""
        ...


class WorkflowStarter(Protocol):
    """Starts a run's workflow under its ``workflow_id`` (RUN-8): Temporal absorbs a repeat."""

    @property
    def available(self) -> bool:
        """``False`` while no workflow client is wired, so the orphan sweep must not act."""
        ...

    async def start(self, workflow_id: str | None, run_input: dict[str, object] | None) -> None:
        """Start once; raises when the engine refuses or is unreachable."""
        ...
