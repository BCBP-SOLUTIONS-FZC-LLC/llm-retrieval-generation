"""Outbound ports of the generation module."""

from __future__ import annotations

from contextlib import AbstractContextManager
from dataclasses import dataclass
from typing import Protocol
from uuid import UUID

from rag.domain.shared.identity import RequestIdentity


class ArtefactContentStore(Protocol):
    """Section text in S3, tenant-prefixed, KMS encryption context carrying ``tenant_id``."""

    async def get(self, tenant_id: UUID, content_ref: str) -> bytes:
        """The bytes at ``content_ref``.

        Raises ``DomainError(ARTEFACT_CONTENT_UNAVAILABLE)`` where the object is missing,
        unreadable or outside ``tenant_id``'s prefix.
        """
        ...

    async def put(self, tenant_id: UUID, key: str, data: bytes) -> None:
        """Write ``data`` under the tenant's prefix. Raises on failure (the caller maps it)."""
        ...

    def section_key(
        self, tenant_id: UUID, artefact_id: UUID, section_id: UUID, version_no: int
    ) -> str:
        """The ``content_ref`` of a section version (tenant-prefixed, ART-9 deletes by prefix)."""
        ...

    def diagnostic_key(self, tenant_id: UUID, artefact_id: UUID, name: str) -> str:
        """A key under the artefact's prefix for retained diagnostics (raw model output, WP-8)."""
        ...

    async def delete_prefix(self, tenant_id: UUID, artefact_id: UUID | None = None) -> int:
        """Delete every object under the artefact's prefix, or the tenant's where None (ART-9).

        Returns the number of objects deleted.
        """
        ...


class StoreProbe(Protocol):
    """GEN-3: is ``llm_generation`` writable? Checked at admission, before any dispatch."""

    async def require_writable(self) -> None:
        """Raises ``DomainError(ARTEFACT_STORE_UNWRITABLE)`` (100 ms, no retry, no breaker)."""
        ...


class TenderGate(Protocol):
    """COL-14: a closed tender takes no new generation work."""

    async def require_open(self, tender_id: UUID) -> None:
        """Raises ``DomainError(TENDER_CLOSED)``; reads ``collections`` ``FOR SHARE``."""
        ...


class TenantBinder(Protocol):
    """Binds the database tenant for a block that runs outside the request context (LD-23).

    The stream body and the producer task do not run inside ``ContextMiddleware``'s binding, so
    the completion transaction takes its tenant from this, never from a context variable that
    outlived its middleware.
    """

    def bind(self, tenant_id: UUID) -> AbstractContextManager[None]:
        """Context manager binding ``tenant_id`` for every statement issued inside it."""
        ...


@dataclass(frozen=True, slots=True)
class UserContext:
    """Prompt layer 3 (``llm-context``): steering text, and the model hint (§22.5)."""

    text: str
    preferred_model: str | None = None


class UserContextUnavailable(Exception):  # noqa: N818
    """IAM ``llm-context`` cannot answer (timeout, breaker open): layer 3 is omitted (§14.3)."""


class UserContextProvider(Protocol):
    """IAM User Profile ``GET /users/:id/llm-context``, cached 300 s, 300 ms, breaker."""

    async def get(self, identity: RequestIdentity) -> UserContext | None:
        """The caller's context, or ``None`` where there is none.

        Raises ``UserContextUnavailable``.
        """
        ...
