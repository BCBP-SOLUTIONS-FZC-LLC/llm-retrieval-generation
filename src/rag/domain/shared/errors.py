"""Error taxonomy of LLD §24: the 32 domain codes, their HTTP status and title.

This module is the single source of the table. The five platform codes (``VALIDATION_FAILED``,
``UNAUTHORIZED``, ``INTERNAL_ERROR``, ``REQUEST_TIMEOUT``, ``FORBIDDEN``) belong to
``fastapicommon`` and are not repeated here. Mapping to RFC 9457 problems is
``presentation/errors.py``.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any


class ErrorCode(StrEnum):
    """Domain ``errorCode`` values (upper snake case)."""

    GRANT_LEVEL_INSUFFICIENT = "GRANT_LEVEL_INSUFFICIENT"
    GENERATION_NOT_PERMITTED = "GENERATION_NOT_PERMITTED"
    TENANT_NOT_ACTIVE = "TENANT_NOT_ACTIVE"
    OPERATOR_REQUIRED = "OPERATOR_REQUIRED"
    COLLECTION_NOT_FOUND = "COLLECTION_NOT_FOUND"
    ARTEFACT_NOT_FOUND = "ARTEFACT_NOT_FOUND"
    RETRIEVAL_SET_NOT_FOUND = "RETRIEVAL_SET_NOT_FOUND"
    FACT_CONFLICT_NOT_FOUND = "FACT_CONFLICT_NOT_FOUND"
    IDEMPOTENCY_KEY_CONFLICT = "IDEMPOTENCY_KEY_CONFLICT"
    OPTIMISTIC_LOCK_CONFLICT = "OPTIMISTIC_LOCK_CONFLICT"
    COLLECTION_NOT_PROVISIONED = "COLLECTION_NOT_PROVISIONED"
    COLLECTION_FROZEN = "COLLECTION_FROZEN"
    COLLECTION_PURGING = "COLLECTION_PURGING"
    WORKFLOW_STATE_CONFLICT = "WORKFLOW_STATE_CONFLICT"
    TENDER_CLOSED = "TENDER_CLOSED"
    GENERATION_NOT_VERIFIED = "GENERATION_NOT_VERIFIED"
    MIGRATION_MISMATCH = "MIGRATION_MISMATCH"
    MIGRATION_INCOMPLETE = "MIGRATION_INCOMPLETE"
    USER_INPUT_OVER_BUDGET = "USER_INPUT_OVER_BUDGET"
    PACK_INVALID = "PACK_INVALID"
    EXTRACTION_SCHEMA_VIOLATION = "EXTRACTION_SCHEMA_VIOLATION"
    CONTENT_CHECK_FAILED = "CONTENT_CHECK_FAILED"
    REQUIRED_INPUT_MISSING = "REQUIRED_INPUT_MISSING"
    QUOTA_EXHAUSTED = "QUOTA_EXHAUSTED"
    ARTEFACT_STORE_UNWRITABLE = "ARTEFACT_STORE_UNWRITABLE"
    ARTEFACT_CONTENT_UNAVAILABLE = "ARTEFACT_CONTENT_UNAVAILABLE"
    RETRIEVAL_FAILED = "RETRIEVAL_FAILED"
    SCOPE_STORE_UNAVAILABLE = "SCOPE_STORE_UNAVAILABLE"
    INFERENCE_UNAVAILABLE = "INFERENCE_UNAVAILABLE"
    RESIDENCY_CAPACITY = "RESIDENCY_CAPACITY"
    RENDERER_UNAVAILABLE = "RENDERER_UNAVAILABLE"
    WORKFLOW_ENGINE_UNAVAILABLE = "WORKFLOW_ENGINE_UNAVAILABLE"


@dataclass(frozen=True, slots=True)
class ErrorSpec:
    """One taxonomy row: code, HTTP status and problem title."""

    code: ErrorCode
    status: int
    title: str

    @property
    def slug(self) -> str:
        """Problem-type slug: the code, lower case with hyphens (§24)."""
        return self.code.value.lower().replace("_", "-")


E = ErrorCode
_ROWS: tuple[ErrorSpec, ...] = (
    ErrorSpec(E.GRANT_LEVEL_INSUFFICIENT, 403, "Grant level insufficient"),
    ErrorSpec(E.GENERATION_NOT_PERMITTED, 403, "Generation not permitted"),
    ErrorSpec(E.TENANT_NOT_ACTIVE, 403, "Tenant not active"),
    ErrorSpec(E.OPERATOR_REQUIRED, 403, "Operator required"),
    ErrorSpec(E.COLLECTION_NOT_FOUND, 404, "Collection not found"),
    ErrorSpec(E.ARTEFACT_NOT_FOUND, 404, "Artefact not found"),
    ErrorSpec(E.RETRIEVAL_SET_NOT_FOUND, 404, "Retrieval set not found"),
    ErrorSpec(E.FACT_CONFLICT_NOT_FOUND, 404, "Fact conflict not found"),
    ErrorSpec(E.IDEMPOTENCY_KEY_CONFLICT, 409, "Idempotency key conflict"),
    ErrorSpec(E.OPTIMISTIC_LOCK_CONFLICT, 409, "Optimistic lock conflict"),
    ErrorSpec(E.COLLECTION_NOT_PROVISIONED, 409, "Collection not provisioned"),
    ErrorSpec(E.COLLECTION_FROZEN, 409, "Collection frozen"),
    ErrorSpec(E.COLLECTION_PURGING, 410, "Collection purging"),
    ErrorSpec(E.WORKFLOW_STATE_CONFLICT, 409, "Workflow state conflict"),
    ErrorSpec(E.TENDER_CLOSED, 409, "Tender closed"),
    ErrorSpec(E.GENERATION_NOT_VERIFIED, 409, "Index generation not verified"),
    ErrorSpec(E.MIGRATION_MISMATCH, 409, "Migration mismatch"),
    ErrorSpec(E.MIGRATION_INCOMPLETE, 409, "Migration incomplete"),
    ErrorSpec(E.USER_INPUT_OVER_BUDGET, 413, "User input over budget"),
    ErrorSpec(E.PACK_INVALID, 422, "Pack invalid"),
    ErrorSpec(E.EXTRACTION_SCHEMA_VIOLATION, 422, "Extraction schema violation"),
    ErrorSpec(E.CONTENT_CHECK_FAILED, 422, "Content check failed"),
    ErrorSpec(E.REQUIRED_INPUT_MISSING, 422, "Required input missing"),
    ErrorSpec(E.QUOTA_EXHAUSTED, 429, "Quota exhausted"),
    ErrorSpec(E.ARTEFACT_STORE_UNWRITABLE, 503, "Artefact store unwritable"),
    ErrorSpec(E.ARTEFACT_CONTENT_UNAVAILABLE, 503, "Artefact content unavailable"),
    ErrorSpec(E.RETRIEVAL_FAILED, 503, "Retrieval failed"),
    ErrorSpec(E.SCOPE_STORE_UNAVAILABLE, 503, "Scope store unavailable"),
    ErrorSpec(E.INFERENCE_UNAVAILABLE, 503, "Inference unavailable"),
    ErrorSpec(E.RESIDENCY_CAPACITY, 503, "Residency capacity unavailable"),
    ErrorSpec(E.RENDERER_UNAVAILABLE, 503, "Renderer unavailable"),
    ErrorSpec(E.WORKFLOW_ENGINE_UNAVAILABLE, 503, "Workflow engine unavailable"),
)

ERROR_TAXONOMY: dict[ErrorCode, ErrorSpec] = {row.code: row for row in _ROWS}
"""Every domain code's spec (§24)."""

PROBLEM_DOMAIN = "retrieval-generation"
"""Problem-type domain: ``{PROBLEM_TYPE_BASE}/retrieval-generation/{slug}`` (§24)."""


class DomainError(Exception):
    """A business failure carrying a §24 code.

    ``detail`` is a sentence the service wrote (never exception text, SQL or tenant data);
    ``extensions`` are RFC 9457 extension members (``recordVersion``, ``pointsExpected``,
    ``pointsReported``); ``retry_after_seconds`` becomes ``Retry-After`` on a 429.
    """

    def __init__(
        self,
        code: ErrorCode,
        detail: str | None = None,
        *,
        extensions: dict[str, Any] | None = None,
        retry_after_seconds: int | None = None,
    ) -> None:
        self.spec = ERROR_TAXONOMY[code]
        self.detail = detail
        self.extensions = extensions or {}
        self.retry_after_seconds = retry_after_seconds
        super().__init__(detail or self.spec.title)

    @property
    def code(self) -> ErrorCode:
        """The §24 error code."""
        return self.spec.code
