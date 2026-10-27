"""Commands of the three synchronous generation routes (R-2, R-3, R-4) after validation."""

from __future__ import annotations

from dataclasses import dataclass, field
from uuid import UUID

from rag.application.retrieval.scope import ScopeSpec
from rag.domain.shared.enums import GenerationMode


@dataclass(frozen=True, slots=True)
class AnswerCommand:
    """R-2. ``scope.tender_id`` is optional: an answer may stand free of a tender (ART-1)."""

    question: str
    scope: ScopeSpec = field(default_factory=ScopeSpec)
    requested_mode: GenerationMode = GenerationMode.ASSISTANCE
    idempotency_key: str | None = None
    correlation_id: str = ""


@dataclass(frozen=True, slots=True)
class DraftCommand:
    """R-3. ``tender_section_ref`` is opaque (ART-4, MD-29)."""

    tender_id: UUID
    tender_section_ref: str
    section_type: str
    scope: ScopeSpec = field(default_factory=ScopeSpec)
    user_prompt: str = ""
    requested_mode: GenerationMode = GenerationMode.ASSISTANCE
    idempotency_key: str | None = None
    correlation_id: str = ""


@dataclass(frozen=True, slots=True)
class ExtractCommand:
    """R-4. ``document_ids`` narrow within the resolved scope; the trigger is echoed (LD-84)."""

    tender_id: UUID
    schema_ref: str
    scope: ScopeSpec = field(default_factory=ScopeSpec)
    document_ids: tuple[UUID, ...] = ()
    trigger_document_version_id: str | None = None
    idempotency_key: str | None = None
    correlation_id: str = ""
