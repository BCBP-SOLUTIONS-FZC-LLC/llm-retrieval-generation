"""Corpus domain failures, each fixed to one §24 code (rendered by ``presentation/errors.py``)."""

from __future__ import annotations

from typing import Any

from rag.domain.shared.errors import DomainError, ErrorCode


class CollectionNotFound(DomainError):
    """``404 COLLECTION_NOT_FOUND``: absent, or not in the caller's scope (CG-8)."""

    def __init__(self, detail: str = "No such collection.") -> None:
        super().__init__(ErrorCode.COLLECTION_NOT_FOUND, detail)


class CollectionFrozen(DomainError):
    """``409 COLLECTION_FROZEN``: the tenant is suspended; the caller holds and retries."""

    def __init__(self, detail: str = "The tenant's corpus is frozen.") -> None:
        super().__init__(ErrorCode.COLLECTION_FROZEN, detail)


class CollectionPurging(DomainError):
    """``410 COLLECTION_PURGING``: the tender or tenant is being purged; the caller discards."""

    def __init__(self, detail: str = "The collection is being purged.") -> None:
        super().__init__(ErrorCode.COLLECTION_PURGING, detail)


class CollectionNotProvisioned(DomainError):
    """``409 COLLECTION_NOT_PROVISIONED``: a reference corpus is never created by an upload."""

    def __init__(self, detail: str = "The reference collection is not provisioned.") -> None:
        super().__init__(ErrorCode.COLLECTION_NOT_PROVISIONED, detail)


class GenerationNotVerified(DomainError):
    """``409 GENERATION_NOT_VERIFIED``: only a ``verified`` generation can be activated (IG-5)."""

    def __init__(self, detail: str = "The index generation is not verified.") -> None:
        super().__init__(ErrorCode.GENERATION_NOT_VERIFIED, detail)


class OptimisticLockConflict(DomainError):
    """``409 OPTIMISTIC_LOCK_CONFLICT``, carrying the current version as ``recordVersion``."""

    def __init__(self, detail: str, current_version: int | None = None) -> None:
        extensions: dict[str, Any] = {}
        if current_version is not None:
            extensions["recordVersion"] = current_version
        super().__init__(ErrorCode.OPTIMISTIC_LOCK_CONFLICT, detail, extensions=extensions)


class MigrationMismatch(DomainError):
    """``409 MIGRATION_MISMATCH``: not the recorded attempt, or not a provisioning target."""

    def __init__(self, detail: str) -> None:
        super().__init__(ErrorCode.MIGRATION_MISMATCH, detail)


class MigrationIncomplete(DomainError):
    """``409 MIGRATION_INCOMPLETE``: fewer points copied than the source holds; no cutover."""

    def __init__(self, points_expected: int, points_reported: int) -> None:
        super().__init__(
            ErrorCode.MIGRATION_INCOMPLETE,
            "The copy reports fewer points than the source holds.",
            extensions={"pointsExpected": points_expected, "pointsReported": points_reported},
        )


class InvalidTransition(RuntimeError):
    """A state-machine edge the LLD does not draw. A defect of the caller, never a client error."""
