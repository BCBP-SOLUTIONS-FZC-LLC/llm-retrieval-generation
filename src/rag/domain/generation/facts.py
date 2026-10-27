"""Document facts and fact conflicts (LLD 4.5.4-4.5.5)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from rag.domain.generation.exceptions import InvariantViolation
from rag.domain.shared.enums import ConflictResolution
from rag.domain.shared.errors import DomainError, ErrorCode


@dataclass(frozen=True, slots=True)
class DocumentFact:
    """An append-only fact; superseded is marked, never overwritten (DF-1, DF-2)."""

    id: UUID
    artefact_id: UUID
    tenant_id: UUID
    fact_key: str
    value: str
    established_by_section_id: UUID
    superseded_at: datetime | None = None
    created_at: datetime | None = None


@dataclass(slots=True)
class FactConflict:
    """A conflict surfaced as a choice, never resolved automatically (FC-1)."""

    id: UUID
    tenant_id: UUID
    artefact_id: UUID
    section_id: UUID
    fact_key: str
    established_value: str
    instruction_value: str
    established_by_section_id: UUID
    resolution: ConflictResolution | None = None
    resolved_at: datetime | None = None
    record_version: int = 1
    created_at: datetime | None = None
    updated_at: datetime | None = None

    def __post_init__(self) -> None:
        if (self.resolution is None) != (self.resolved_at is None):
            raise InvariantViolation("FC", "resolution and resolved_at are set together")


@dataclass(frozen=True, slots=True)
class ConflictOutcome:
    """What a resolution entails (§5.7.10): the route applies it, it never regenerates (LD-26)."""

    resolution: ConflictResolution
    supersede_fact: bool
    insert_value: str | None
    flag_sections: bool

    @property
    def proceeds(self) -> bool:
        """``keep_established`` and ``update_fact`` let the client re-issue the regeneration.

        ``dismiss`` ends the interaction (LD-32).
        """
        return self.resolution is not ConflictResolution.DISMISS


def resolve_conflict(
    conflict: FactConflict, resolution: ConflictResolution, now: datetime
) -> ConflictOutcome:
    """Close an open conflict (REGEN-6: nothing contradicts a fact without an explicit choice).

    Guarded on the conflict's own state: a resolved conflict is ``409 WORKFLOW_STATE_CONFLICT``
    carrying the recorded resolution, so a second reviewer overwrites nothing.
    """
    if conflict.resolution is not None:
        raise DomainError(
            ErrorCode.WORKFLOW_STATE_CONFLICT,
            "The conflict is already resolved.",
            extensions={"resolution": conflict.resolution.value},
        )
    conflict.resolution = resolution
    conflict.resolved_at = now
    update = resolution is ConflictResolution.UPDATE_FACT
    return ConflictOutcome(
        resolution=resolution,
        supersede_fact=update,
        insert_value=conflict.instruction_value if update else None,
        flag_sections=update,
    )
