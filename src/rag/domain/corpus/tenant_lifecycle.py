"""``TenantLifecycle`` (§4.7.5, TL-1..TL-3) and the IAM status mapping (§12.4, LD-46)."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime
from enum import StrEnum
from uuid import UUID

from rag.domain.corpus.errors import InvalidTransition


class TenantState(StrEnum):
    """``tenant_lifecycle.state``; no row means the tenant is served."""

    FROZEN = "frozen"
    PURGED = "purged"


class TenantAction(StrEnum):
    """What a ``TenantStateChanged`` status does here."""

    FREEZE = "freeze"
    RESTORE = "restore"
    IGNORE = "ignore"


_ACTIONS = {
    "suspended": TenantAction.FREEZE,
    "trial_expired": TenantAction.FREEZE,
    "trial": TenantAction.RESTORE,
    "active": TenantAction.RESTORE,
    # past_due is no freeze, cancelled keeps retrieval, offboarded is the purge's (EVT-15)
    "past_due": TenantAction.IGNORE,
    "cancelled": TenantAction.IGNORE,
    "offboarded": TenantAction.IGNORE,
}


def action_for_status(status: str) -> TenantAction:
    """The mapping of IAM's seven statuses (LD-46); an unknown status changes nothing."""
    return _ACTIONS.get(status, TenantAction.IGNORE)


@dataclass(frozen=True, slots=True)
class TenantLifecycle:
    """One row for a frozen or purged tenant."""

    tenant_id: UUID
    state: TenantState
    changed_at: datetime
    cause: str | None = None
    record_version: int = 1

    @property
    def purged(self) -> bool:
        """``purged`` is terminal and permanent (TL-1)."""
        return self.state is TenantState.PURGED

    def purge(self, at: datetime, cause: str | None = None) -> TenantLifecycle:
        """To ``purged``; a repeat keeps the first ``changed_at`` (the soak clock)."""
        if self.purged:
            return self
        return replace(self, state=TenantState.PURGED, changed_at=at, cause=cause or self.cause)

    def refreeze(self, cause: str | None) -> TenantLifecycle:
        """A repeated freeze updates the cause only; the reverse of ``purge`` is refused (TL-2)."""
        if self.purged:
            raise InvalidTransition("tenant_lifecycle purged is terminal")
        return replace(self, cause=cause)

    def soak_elapsed(self, now: datetime, soak_days: int) -> bool:
        """Phase two of a purge runs ``TENANT_PURGE_SOAK_DAYS`` after ``changed_at`` (EVT-15)."""
        return self.purged and (now - self.changed_at).days >= soak_days
