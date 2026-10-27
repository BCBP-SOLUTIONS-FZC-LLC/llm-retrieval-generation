"""Enumerated types of LLD §4.2 and §4.7.

Values are the exact Postgres enum labels, so a member round-trips through the database as
its string value. ``GrantLevel`` and ``EvidenceTier`` are ordered by declaration order, which
is load-bearing: the evidence roll-up is ``min()`` and the grant clamp (SCOPE-8) clamps down.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any


class OrderedStrEnum(StrEnum):
    """A string enum ordered by member declaration order (lowest first)."""

    @property
    def rank(self) -> int:
        """Zero-based position in declaration order."""
        return list(type(self)).index(self)

    def __lt__(self, other: Any) -> bool:
        return self._compare(other, lambda a, b: a < b)

    def __le__(self, other: Any) -> bool:
        return self._compare(other, lambda a, b: a <= b)

    def __gt__(self, other: Any) -> bool:
        return self._compare(other, lambda a, b: a > b)

    def __ge__(self, other: Any) -> bool:
        return self._compare(other, lambda a, b: a >= b)

    def _compare(self, other: Any, op: Any) -> bool:
        if type(other) is not type(self):
            return NotImplemented  # type: ignore[no-any-return]
        return bool(op(self.rank, other.rank))


class EvidenceTier(OrderedStrEnum):
    """Evidence tier; ``NOT_COMPUTED`` sorts lowest so the roll-up is ``min()`` (MD-24, LD-56)."""

    NOT_COMPUTED = "NOT_COMPUTED"
    INSUFFICIENT = "INSUFFICIENT"
    PARTIAL = "PARTIAL"
    SUPPORTED = "SUPPORTED"


class GrantLevel(OrderedStrEnum):
    """Collection grant level; a clamp takes the ``min()`` (SCOPE-8, LD-21)."""

    SEARCH = "search"
    ANSWER = "answer"
    GENERATE = "generate"


class ArtefactKind(StrEnum):
    """Kind of generated artefact."""

    ANSWER = "answer"
    SECTION_DRAFT = "section_draft"
    DOCUMENT = "document"
    EXTRACTION = "extraction"


class ArtefactStatus(StrEnum):
    """Artefact lifecycle status."""

    PENDING = "pending"
    GENERATING = "generating"
    COMPLETE = "complete"
    CANCELLED = "cancelled"
    FAILED = "failed"
    SUPERSEDED = "superseded"


class ArtefactArchiveState(StrEnum):
    """Artefact archive state (ART-7)."""

    LIVE = "live"
    ARCHIVED = "archived"


class AuditActorType(StrEnum):
    """The Audit Log's actor enum, mirrored (§4.6.3)."""

    USER = "user"
    SERVICE_ACCOUNT = "service_account"
    IAM_SYSTEM = "iam_system"
    ANONYMOUS = "anonymous"


class ArtefactOrigin(StrEnum):
    """How an artefact version came to exist."""

    ORIGINAL = "original"
    REGENERATION = "regeneration"
    COHERENCE_REPAIR = "coherence_repair"


class DeliveryState(StrEnum):
    """Whether generated text reached the client."""

    DELIVERED = "delivered"
    DISCONNECTED = "disconnected"
    NOT_APPLICABLE = "not_applicable"


class EligibilityState(StrEnum):
    """Domain Profile generation eligibility (§7.6)."""

    GENERATABLE = "GENERATABLE"
    PARTIALLY_GENERATABLE = "PARTIALLY_GENERATABLE"
    NOT_GENERATABLE = "NOT_GENERATABLE"


class GenerationMode(StrEnum):
    """Generation mode."""

    ASSISTANCE = "assistance"
    AUTOMATION = "automation"


class RetrievalVerdict(StrEnum):
    """Retrieval sufficiency verdict (MD-14)."""

    SUFFICIENT = "sufficient"
    INSUFFICIENT = "insufficient"
    FAILED = "failed"


class CompletenessVerdict(StrEnum):
    """Completeness verdict (§8.2)."""

    COMPLETE = "complete"
    PARTIAL = "partial"
    INSUFFICIENT = "insufficient"
    NOT_COMPUTED = "not_computed"


class ClaimOutcome(StrEnum):
    """Per-claim verification outcome (MD-16)."""

    SUPPORTED = "supported"
    UNSUPPORTED = "unsupported"
    MISCITED = "miscited"
    GAP = "gap"


class SectionState(StrEnum):
    """Long-form section state."""

    PENDING = "pending"
    GENERATING = "generating"
    COMPLETE = "complete"
    GAP = "gap"
    FAILED = "failed"


class RefinerOutcome(StrEnum):
    """Query/instruction refiner outcome."""

    SUCCESS = "success"
    DEGRADED = "degraded"
    DISABLED = "disabled"


class UserAction(StrEnum):
    """What the user did with a generated artefact."""

    ACCEPTED = "accepted"
    EDITED = "edited"
    DISCARDED = "discarded"
    UNKNOWN = "unknown"


class InstructionType(StrEnum):
    """Regeneration instruction type (§10.2)."""

    STYLE = "style"
    ADD = "add"
    REMOVE = "remove"
    CORRECT = "correct"
    EXPAND = "expand"


class ConflictResolution(StrEnum):
    """Fact-conflict resolution (D-35)."""

    KEEP_ESTABLISHED = "keep_established"
    UPDATE_FACT = "update_fact"
    DISMISS = "dismiss"


class RunStatus(StrEnum):
    """Generation run status."""

    PENDING = "pending"
    OUTLINE_BUILDING = "outline_building"
    AWAITING_OUTLINE_APPROVAL = "awaiting_outline_approval"
    GENERATING = "generating"
    SUSPENDED = "suspended"
    COMPLETE = "complete"
    FAILED = "failed"
    CANCELLED = "cancelled"


class RunFailureReason(StrEnum):
    """Why a run failed (lower snake case, unlike error codes)."""

    SCOPE_REVOKED = "scope_revoked"
    QUOTA_EXHAUSTED = "quota_exhausted"
    INFERENCE_UNAVAILABLE = "inference_unavailable"
    RESIDENCY_CAPACITY = "residency_capacity"
    ARTEFACT_STORE_UNWRITABLE = "artefact_store_unwritable"
    GATE_EXPIRED = "gate_expired"
    INTERNAL = "internal"


class OutlineState(StrEnum):
    """Outline state of a long-form run."""

    NONE = "none"
    BUILDING = "building"
    READY = "ready"
    APPROVED = "approved"
    REVISING = "revising"


class RunStage(StrEnum):
    """The run workflow's phase names (LD-77)."""

    OUTLINE = "outline"
    STAGE1 = "stage1"
    STAGE2 = "stage2"
    COHERENCE = "coherence"
    EXTRACT = "extract"


class CoherenceVerdict(StrEnum):
    """Document coherence verdict."""

    NOT_COMPUTED = "not_computed"
    CONSISTENT = "consistent"
    CONFLICTED = "conflicted"


class CollectionType(StrEnum):
    """Collection type (§4.7)."""

    TENDER = "tender"
    TENANT = "tenant"
    REFERENCE = "reference"


class CollectionStatus(StrEnum):
    """Collection status (§4.7.1)."""

    PROVISIONING = "provisioning"
    ACTIVE = "active"
    FROZEN = "frozen"
    PURGING = "purging"
    MIGRATION_FAILED = "migration_failed"
    SUPERSEDED = "superseded"


class IndexGenerationStatus(StrEnum):
    """Index generation status (§4.7.3)."""

    REGISTERED = "registered"
    BUILDING = "building"
    VERIFIED = "verified"
    ACTIVE = "active"
    FAILED = "failed"
    RETIRED = "retired"


class GrantSubjectType(StrEnum):
    """Grant subject type (LD-79: only users)."""

    USER = "user"
