"""``RetentionPolicy`` (§4.7.4, RP-1, LD-76)."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime
from uuid import UUID

from rag.domain.corpus.errors import OptimisticLockConflict


@dataclass(frozen=True, slots=True)
class RetentionPolicy:
    """One ``retention_policies`` row."""

    collection_id: UUID
    tenant_id: UUID
    retain_days: int
    legal_hold: bool
    updated_by: UUID
    record_version: int = 1
    updated_at: datetime | None = None

    def __post_init__(self) -> None:
        """``retain_days`` is positive (``CHECK (retain_days > 0)``)."""
        if self.retain_days <= 0:
            raise ValueError("retain_days must be positive")

    @property
    def swept(self) -> bool:
        """RP-1: a collection under legal hold is excluded from the sweep."""
        return not self.legal_hold

    def updated(
        self, *, retain_days: int, legal_hold: bool, updated_by: UUID, expected_version: int
    ) -> RetentionPolicy:
        """Apply a P-6 update; a stale ``record_version`` is a conflict carrying the current one."""
        if expected_version != self.record_version:
            raise OptimisticLockConflict(
                "The retention policy changed; retry with its current record_version.",
                self.record_version,
            )
        return replace(self, retain_days=retain_days, legal_hold=legal_hold, updated_by=updated_by)
