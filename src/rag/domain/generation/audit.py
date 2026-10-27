"""The artefact audit chain (LLD 4.6.3): entry shape, canonical JSON and hashes.

The database computes ``seq``, ``prev_hash``, ``payload_hash`` and ``entry_hash`` (AC-2) in
``audit_chain_append()``; this module is its Python twin, used to verify a chain (the 21.2
verifier) and to build ``content_digest`` (AC-8). The canonical form: sorted keys, no
whitespace, UTF-8, over every column above ``payload_hash`` (``seq`` included, so an entry's
position is tamper-evident); ``created_at`` and the hashes are not part of it.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from rag.domain.generation.exceptions import InvariantViolation
from rag.domain.shared.enums import AuditActorType

SOURCE_SERVICE = "llm-rag-service"
METADATA_MAX_BYTES = 8192

# Entry types (4.6.3 table): (entry_type, action)
GENERATION_COMPLETED = ("llm.generation.completed", "create")
SECTION_REGENERATED = ("llm.section.regenerated", "create")
GENERATION_FAILED = ("llm.generation.failed", "update")
GENERATION_CANCELLED = ("llm.generation.cancelled", "update")
FACT_CONFLICT_RESOLVED = ("llm.fact_conflict.resolved", "update")
SECTION_FLAGGED = ("llm.section.flagged", "update")
ARTEFACT_ARCHIVED = ("llm.artefact.archived", "update")
ARTEFACT_PURGED = ("llm.artefact.purged", "delete")
CROSS_TENANT_ACCESS = ("llm.cross_tenant_access", "update")
"""W-8 (§5.7.14, §15.3). Not in the §4.6.3 table: the platform Audit Log takes
``cross_tenant_access`` by direct write (its AL-5), which no client here reaches, so the chain
carries the record."""


def canonical_json(value: Any) -> str:
    """Sorted keys, no whitespace, non-ASCII kept (UTF-8 on encode). Metadata numbers: integers."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _utc_text(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


@dataclass(frozen=True, slots=True)
class AuditEntryDraft:
    """An entry as the writer supplies it: everything but the database-owned columns."""

    tenant_id: UUID
    occurred_at: datetime
    entry_type: str
    action: str
    actor_type: AuditActorType
    actor_id: UUID | None
    artefact_id: UUID
    source_event_type: str
    source_event_id: str
    target_type: str | None = None
    target_id: str | None = None
    trace_id: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    source_service: str = SOURCE_SERVICE

    def __post_init__(self) -> None:
        anonymous = self.actor_type is AuditActorType.ANONYMOUS
        if anonymous != (self.actor_id is None):
            raise InvariantViolation("AC", "actor_id is NULL iff actor_type = 'anonymous'")
        if self.occurred_at.tzinfo is None:
            raise InvariantViolation("AC", "occurred_at must be timezone-aware")
        if len(json.dumps(self.metadata, sort_keys=True).encode()) > METADATA_MAX_BYTES:
            raise InvariantViolation("AC-7", f"metadata is at most {METADATA_MAX_BYTES} bytes")


@dataclass(frozen=True, slots=True)
class AuditEntry:
    """A stored chain entry."""

    draft: AuditEntryDraft
    seq: int
    payload_hash: bytes
    prev_hash: bytes | None
    entry_hash: bytes


def payload_document(draft: AuditEntryDraft, seq: int) -> dict[str, Any]:
    """The object the database hashes: every column above ``payload_hash`` (AC-2)."""
    return {
        "tenant_id": str(draft.tenant_id),
        "seq": seq,
        "occurred_at": _utc_text(draft.occurred_at),
        "entry_type": draft.entry_type,
        "action": draft.action,
        "actor_type": draft.actor_type.value,
        "actor_id": None if draft.actor_id is None else str(draft.actor_id),
        "artefact_id": str(draft.artefact_id),
        "target_type": draft.target_type,
        "target_id": draft.target_id,
        "source_service": draft.source_service,
        "source_event_type": draft.source_event_type,
        "source_event_id": draft.source_event_id,
        "trace_id": draft.trace_id,
        "metadata": draft.metadata,
    }


def compute_payload_hash(draft: AuditEntryDraft, seq: int) -> bytes:
    """``sha256`` over the canonical JSON of the entry's columns."""
    return hashlib.sha256(canonical_json(payload_document(draft, seq)).encode("utf-8")).digest()


def compute_entry_hash(prev_hash: bytes | None, payload_hash: bytes) -> bytes:
    """``sha256(coalesce(prev_hash, '') || payload_hash)`` (AC-2)."""
    return hashlib.sha256((prev_hash or b"") + payload_hash).digest()


def content_digest(pairs: Iterable[tuple[UUID, bytes]]) -> str:
    """AC-8: SHA-256 of ``section_version_id:content_sha256`` pairs sorted by id, hex.

    Each pair is rendered ``<uuid>:<lowercase hex sha256>``; the pairs are newline-joined.
    """
    lines = [f"{sv_id}:{sha.hex()}" for sv_id, sha in sorted(pairs, key=lambda p: str(p[0]))]
    return hashlib.sha256("\n".join(lines).encode("utf-8")).hexdigest()


def first_break(entries: Sequence[AuditEntry]) -> int | None:
    """Verify one tenant's chain in ``seq`` order; the first broken ``seq``, else ``None``.

    Checks AC-3 (gapless from 1), AC-4 (``prev_hash`` is the previous ``entry_hash``, NULL only
    at 1) and AC-2 (both hashes recompute).
    """
    prev: AuditEntry | None = None
    for position, entry in enumerate(sorted(entries, key=lambda e: e.seq), start=1):
        expected_prev = None if prev is None else prev.entry_hash
        payload = compute_payload_hash(entry.draft, entry.seq)
        if (
            entry.seq != position
            or entry.prev_hash != expected_prev
            or entry.payload_hash != payload
            or entry.entry_hash != compute_entry_hash(entry.prev_hash, payload)
        ):
            return entry.seq
        prev = entry
    return None
