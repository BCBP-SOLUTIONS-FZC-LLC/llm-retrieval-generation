"""Event contracts of §12: type names, topics, and the payload of every type this service publishes.

Plain frozen dataclasses (the application layer may import nothing third-party); the
infrastructure layer derives validation and JSON Schema from them with pydantic
(``infrastructure.events.schemas``). A field that is ``None`` is *absent* from the wire unless it
is listed in ``NULLABLE`` (§12.3: "absent on it" is not the same as ``null``). Payloads carry
identifiers, versions and status only (EVT-7): there is no field in which content could travel.

Event type names are PascalCase (LD-49) and load-bearing: they are the SNS ``EventType`` attribute
and the routing key of the relay's publisher (§18.3).
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass, field
from enum import Enum
from typing import Any
from uuid import UUID

from rag.application.shared.events import DomainEvent
from rag.domain.corpus.collection import REFERENCE, TENANT_WIDE
from rag.domain.shared.enums import (
    ArtefactKind,
    ArtefactOrigin,
    CoherenceVerdict,
    CompletenessVerdict,
    DeliveryState,
    EligibilityState,
    EvidenceTier,
    GenerationMode,
    OutlineState,
    RunFailureReason,
    RunStage,
    RunStatus,
    SectionState,
)

SOURCE = "llm-rag-service"
"""Envelope ``source``: a constant, never carrying an environment (§12.2)."""

TOPIC_GENERATION = "llm-generation-events"
TOPIC_INDEX = "llm-index-events"

# --- published types ---------------------------------------------------------------------------
GENERATION_STARTED = "GenerationStarted"
GENERATION_GAP_REPORTED = "GenerationGapReported"
OUTLINE_READY = "OutlineReady"
SECTION_COMPLETED = "SectionCompleted"
GENERATION_PROGRESSED = "GenerationProgressed"
GENERATION_COMPLETED = "GenerationCompleted"
GENERATION_FAILED = "GenerationFailed"
GENERATION_CANCELLED = "GenerationCancelled"
ARTEFACT_REGENERATED = "ArtefactRegenerated"
EXTRACTION_COMPLETED = "ExtractionCompleted"

INDEX_GENERATION_ACTIVATED = "IndexGenerationActivated"
INDEX_GENERATION_FAILED = "IndexGenerationFailed"
CORPUS_CONFLICT_DETECTED = "CorpusConflictDetected"
COLLECTION_PROVISIONED = "CollectionProvisioned"
COLLECTION_PROMOTED = "CollectionPromoted"
COLLECTION_STATUS_CHANGED = "CollectionStatusChanged"
PROFILE_CACHE_REFRESHED = "ProfileCacheRefreshed"

INDEX_EVENT_TYPES = frozenset(
    {
        INDEX_GENERATION_ACTIVATED,
        INDEX_GENERATION_FAILED,
        CORPUS_CONFLICT_DETECTED,
        COLLECTION_PROVISIONED,
        COLLECTION_PROMOTED,
        COLLECTION_STATUS_CHANGED,
        PROFILE_CACHE_REFRESHED,
    }
)
"""Routed to ``llm-index-events`` by name; every other type goes to ``llm-generation-events``."""

# --- consumed types (the publishers' names; Tender's are unconfirmed, §20 OQ-73/74) -------------
DOCUMENT_PURGED = "DocumentPurged"
DOMAIN_PROFILE_PUBLISHED = "DomainProfilePublished"
TENANT_STATE_CHANGED = "TenantStateChanged"
TENANT_MEMBERSHIPS_PURGED = "TenantMembershipsPurged"
TENDER_RESTRICTED = "TenderRestricted"
TENDER_PURGED = "TenderPurged"
TENDER_TERMINAL_TYPES = frozenset({"TenderDeclined", "TenderWon", "TenderLost", "TenderWithdrawn"})
"""ASSUMED names of the four terminal outcomes (LD-89); Tender HLD §13.5 names transitions only."""
TENANT_QUOTA_RESTORED = "TenantQuotaRestored"

# consumer names in the ``processed_events`` ledger (§12.4, EVT-6)
CONSUMER_PURGE_PROJECTION = "purge_projection"
CONSUMER_PACK_INVALIDATION = "pack_invalidation"
CONSUMER_MEMBERSHIP = "membership"
CONSUMER_TENDER_LIFECYCLE = "tender_lifecycle"
CONSUMER_QUOTA_RESUME = "quota_resume"
DESTRUCTIVE_CONSUMERS = frozenset(
    {
        CONSUMER_PURGE_PROJECTION,
        CONSUMER_MEMBERSHIP,
        CONSUMER_TENDER_LIFECYCLE,
    }
)
"""Ledger names kept past the ordinary eight days (EVT-16)."""


def tender_subject(tender_id: UUID | str | None) -> str:
    """``tenders/<tender_id>`` where the event is Tender-bound, ``""`` (absent) otherwise (LD-27).

    The tenant-wide and reference sentinels are not tenders.
    """
    if tender_id is None or UUID(str(tender_id)) in {TENANT_WIDE, REFERENCE}:
        return ""
    return f"tenders/{tender_id}"


@dataclass(frozen=True, slots=True)
class Recipient:
    """``{kind, id}`` echoed from the request that started the run (LD-83).

    ``kind`` is ``user`` or ``department``; the request refuses any other.
    """

    kind: str
    id: str


def _opt() -> Any:
    return field(default=None)


# --- llm.generation.events ---------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class GenerationStarted:
    """Every admission, synchronous or not, in the shell's transaction (ASYNC-2, RUN-1, GEN-10)."""

    artefact_id: str
    generation_id: str
    principal_id: str
    kind: ArtefactKind
    capability: str
    synchronous: bool
    requested_mode: GenerationMode
    effective_mode: GenerationMode
    eligibility_state: EligibilityState
    clamped: bool
    domain_profile_version: str
    language_pack_version: str
    tender_id: str | None = _opt()
    target_ref: str | None = _opt()
    clamp_reason: str | None = _opt()
    recipients: list[Recipient] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class GenerationGapReported:
    """A blocking required-input failure (§6.9). Entity *keys* only, never values."""

    artefact_id: str
    principal_id: str
    section_ref: str
    missing_roles: list[str]
    unresolved_entities: list[str]
    empty_collections: list[str]
    blocking: bool
    tender_id: str | None = _opt()
    recipients: list[Recipient] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class OutlineReady:
    """The workflow suspends on the human gate (§9.4)."""

    artefact_id: str
    principal_id: str
    outline_version: int
    section_count: int
    gate_expires_at: str
    tender_id: str | None = _opt()
    recipients: list[Recipient] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class SectionCompleted:
    """Once per section. A gap or failure has no version, so those fields are absent (AS-7)."""

    artefact_id: str
    principal_id: str
    section_ref: str
    state: SectionState
    origin: ArtefactOrigin
    sections_complete: int
    sections_total: int
    generation_id: str | None = _opt()
    tender_id: str | None = _opt()
    section_version_id: str | None = _opt()
    evidence_tier: EvidenceTier | None = _opt()
    completeness: CompletenessVerdict | None = _opt()
    retrieval_set_id: str | None = _opt()
    superseded_version_ids: list[str] | None = _opt()
    """Present only on a regeneration, possibly empty (LD-31, AV-6)."""
    recipients: list[Recipient] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class GenerationProgressed:
    """Every stage transition of an async run (LD-51); ``progress_seq`` orders it (EVT-13)."""

    artefact_id: str
    generation_id: str
    principal_id: str
    progress_seq: int
    run_status: RunStatus
    sections_complete: int
    sections_total: int
    outline_state: OutlineState
    observed_at: str
    tender_id: str | None = _opt()
    stage: RunStage | None = _opt()
    reason: RunFailureReason | None = _opt()
    """Why the run is ``suspended`` (``quota_exhausted`` today); absent in every other state."""
    recipients: list[Recipient] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class GenerationCompleted:
    """A version was written; short-form included (GEN-10). No approval state (AOB-1, EVT-11)."""

    artefact_id: str
    artefact_version_id: str
    version_no: int
    principal_id: str
    kind: ArtefactKind
    minted_reason: str
    origin: ArtefactOrigin
    evidence_tier: EvidenceTier
    completeness: CompletenessVerdict
    delivery: DeliveryState
    section_count: int
    generation_id: str | None = _opt()
    tender_id: str | None = _opt()
    coherence_verdict: CoherenceVerdict | None = _opt()
    recipients: list[Recipient] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class GenerationFailed:
    """A workflow ended without a version. ``reason`` is ``run_failure_reason`` exactly."""

    artefact_id: str
    principal_id: str
    reason: RunFailureReason
    sections_complete: int
    sections_total: int
    sections_durable: bool
    generation_id: str | None = _opt()
    tender_id: str | None = _opt()
    failed_at_stage: RunStage | None = _opt()
    recipients: list[Recipient] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class GenerationCancelled:
    """W-5. A cancellation is not a failure, so no ``run_failure_reason``."""

    artefact_id: str
    generation_id: str
    principal_id: str
    reason: str
    sections_complete: int
    sections_total: int
    sections_durable: bool
    tender_id: str | None = _opt()
    recipients: list[Recipient] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class ArtefactRegenerated:
    """A regeneration minted a *document* version (a section one is ``SectionCompleted``)."""

    artefact_id: str
    artefact_version_id: str
    version_no: int
    base_version_id: str
    principal_id: str
    scope: str
    origin: ArtefactOrigin
    evidence_tier: EvidenceTier
    completeness: CompletenessVerdict
    tender_id: str | None = _opt()
    recipients: list[Recipient] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class ExtractionCompleted:
    """R-4's and W-6's write transaction (LD-84)."""

    artefact_id: str
    artefact_version_id: str
    generation_id: str
    principal_id: str
    trigger_document_version_id: str
    schema_version: str
    evidence_tier: EvidenceTier
    tender_id: str | None = _opt()
    recipients: list[Recipient] = field(default_factory=list)


# --- llm.index.events --------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class IndexGenerationActivated:
    """C-I4's cutover transaction."""

    collection_id: str
    tender_id: str
    generation: int
    embedding_profile: str
    chunker_version: str
    activated_at: str
    previous_generation: int | None = None


@dataclass(frozen=True, slots=True)
class IndexGenerationFailed:
    """The ``failed`` row's transaction (LD-67). Nothing writes one in v1."""

    collection_id: str
    tender_id: str
    generation: int
    failed_checks: list[Any]
    serving_generation: int


@dataclass(frozen=True, slots=True)
class ConflictEntry:
    """One disagreement: a fact key and the identifiers behind it, never the text (CONF-2)."""

    fact_key: str
    severity: str
    chunk_ids: list[str]
    document_ids: list[str]


@dataclass(frozen=True, slots=True)
class CorpusConflictDetected:
    """Declared in the AsyncAPI file and **not published in v1** (LD-64, §9.8)."""

    collection_id: str
    tender_id: str
    index_generation: int
    checked_at: str
    conflicts: list[ConflictEntry]
    conflict_count: int
    truncated: bool


@dataclass(frozen=True, slots=True)
class CollectionProvisioned:
    """C-I1 and the ``TenderRestricted`` consumer's creation."""

    collection_id: str
    tender_id: str
    type: str
    restricted: bool
    status: str
    promoted_from: str | None = None


@dataclass(frozen=True, slots=True)
class CollectionPromoted:
    """A promotion target; always names the collection the tender moved out of."""

    collection_id: str
    tender_id: str
    type: str
    restricted: bool
    status: str
    promoted_from: str | None = None


@dataclass(frozen=True, slots=True)
class CollectionStatusChanged:
    """Every status change but creation (LD-78); ``reason`` is the §12.3 five."""

    collection_id: str
    tender_id: str
    type: str
    from_status: str
    to_status: str
    reason: str


@dataclass(frozen=True, slots=True)
class ProfileCacheRefreshed:
    """An operational signal about this service's own pack cache (§12.3)."""

    profile_ref: str
    domain: str
    language: str
    concern: str
    to_version: str
    triggered_by_event_id: str
    from_version: str | None = _opt()


PUBLISHED: dict[str, type] = {
    GENERATION_STARTED: GenerationStarted,
    GENERATION_GAP_REPORTED: GenerationGapReported,
    OUTLINE_READY: OutlineReady,
    SECTION_COMPLETED: SectionCompleted,
    GENERATION_PROGRESSED: GenerationProgressed,
    GENERATION_COMPLETED: GenerationCompleted,
    GENERATION_FAILED: GenerationFailed,
    GENERATION_CANCELLED: GenerationCancelled,
    ARTEFACT_REGENERATED: ArtefactRegenerated,
    EXTRACTION_COMPLETED: ExtractionCompleted,
    INDEX_GENERATION_ACTIVATED: IndexGenerationActivated,
    INDEX_GENERATION_FAILED: IndexGenerationFailed,
    CORPUS_CONFLICT_DETECTED: CorpusConflictDetected,
    COLLECTION_PROVISIONED: CollectionProvisioned,
    COLLECTION_PROMOTED: CollectionPromoted,
    COLLECTION_STATUS_CHANGED: CollectionStatusChanged,
    PROFILE_CACHE_REFRESHED: ProfileCacheRefreshed,
}
"""The seventeen ``send`` operations of the AsyncAPI file (§12.6). EVT-9: nothing is published
that is not here."""

NULLABLE: dict[str, frozenset[str]] = {
    INDEX_GENERATION_ACTIVATED: frozenset({"previous_generation"}),
    COLLECTION_PROVISIONED: frozenset({"promoted_from"}),
    COLLECTION_PROMOTED: frozenset({"promoted_from"}),
}
"""Fields that are ``null`` rather than absent when unset (the §12.3 examples)."""

GENERATION_EVENT_TYPES = frozenset(PUBLISHED) - INDEX_EVENT_TYPES


def _plain(value: Any) -> Any:
    if isinstance(value, Enum):
        return value.value
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return {f.name: _plain(getattr(value, f.name)) for f in dataclasses.fields(value)}
    if isinstance(value, list | tuple):
        return [_plain(v) for v in value]
    return value


def payload_of(payload: Any) -> dict[str, Any]:
    """The wire ``data`` of a contract dataclass: enums as strings, unset optionals absent."""
    name = type(payload).__name__
    keep_null = NULLABLE.get(name, frozenset())
    out: dict[str, Any] = {}
    for f in dataclasses.fields(payload):
        value = getattr(payload, f.name)
        if value is None and f.name not in keep_null:
            continue
        out[f.name] = _plain(value)
    return out


def build_event(
    tenant_id: UUID,
    subject: str,
    payload: Any,
    *,
    correlation_id: str = "",
) -> DomainEvent:
    """A ``DomainEvent`` for ``payload``; its type is the dataclass name and must be registered."""
    name = type(payload).__name__
    if PUBLISHED.get(name) is not type(payload):
        raise ValueError(f"{name} is not a registered event contract (EVT-9)")
    return DomainEvent(name, tenant_id, subject, payload_of(payload), correlation_id)


# --- consumed payloads (fields read, §12.4) -----------------------------------------------------
@dataclass(frozen=True, slots=True)
class DocumentPurged:
    """Ingestion's erasure of a document: all four fields are in the match (EVT-1)."""

    document_id: UUID
    document_version: int
    collection_id: UUID
    tenant_id: UUID | None = None


@dataclass(frozen=True, slots=True)
class DomainProfilePublished:
    """A republished profile: clears this service's cache, never a pinned workflow (EVT-4)."""

    domain: str
    language: str
    concern: str
    version: str
    tenant_id: UUID | None = None


@dataclass(frozen=True, slots=True)
class TenantStateChanged:
    """IAM's relay of one of seven statuses; ``cause`` is free text, never branched on."""

    status: str
    cause: str | None = None
    tenant_id: UUID | None = None


@dataclass(frozen=True, slots=True)
class TenantMembershipsPurged:
    """IAM's tenant-offboarding cascade signal; ``actor_id`` is optional."""

    actor_id: str | None = None
    tenant_id: UUID | None = None


@dataclass(frozen=True, slots=True)
class TenderEvent:
    """Any Tender event this service reads: restricted, purged, or a terminal outcome."""

    tender_id: UUID
    tenant_id: UUID | None = None


# ASSUMPTION (docs/implementation-plan.md): Commercial HLD v0.48 does not give the ``resource``
# value of the quota events. Its add-on event and table use ``llm_tokens`` (§8.6.2, §9.4, §7.3);
# the LLD (LD-70) assumed ``tokens``. Both name the token quota, so both act.
TOKEN_QUOTA_RESOURCES = frozenset({"tokens", "llm_tokens"})


@dataclass(frozen=True, slots=True)
class TenantQuotaRestored:
    """Only a token ``resource`` acts, because only a token quota suspends a run (LD-70).

    ``cause`` is ``window_reset`` or ``limit_raised`` (Commercial HLD v0.48 §9.4) and is not read.
    """

    resource: str
    cause: str | None = None
    tenant_id: UUID | None = None


CONSUMED: dict[str, type] = {
    DOCUMENT_PURGED: DocumentPurged,
    DOMAIN_PROFILE_PUBLISHED: DomainProfilePublished,
    TENANT_STATE_CHANGED: TenantStateChanged,
    TENANT_MEMBERSHIPS_PURGED: TenantMembershipsPurged,
    TENDER_RESTRICTED: TenderEvent,
    TENDER_PURGED: TenderEvent,
    TENANT_QUOTA_RESTORED: TenantQuotaRestored,
    **dict.fromkeys(TENDER_TERMINAL_TYPES, TenderEvent),
}
"""Consumed type name -> the fields read from its payload."""
