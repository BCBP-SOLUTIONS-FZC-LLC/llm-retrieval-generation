"""``CollectionGrant``: the last overlay answer this service recorded (CG-2, CG-11)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from rag.domain.shared.enums import GrantLevel, GrantSubjectType


@dataclass(frozen=True, slots=True)
class CollectionGrant:
    """A projection row, read by P-3 only. Authorization never reads it (CG-11)."""

    id: UUID
    collection_id: UUID
    tenant_id: UUID
    subject_type: GrantSubjectType
    subject_id: UUID
    level: GrantLevel
    projected_at: datetime
    source: str = "iam_overlay"
    expires_at: datetime | None = None
