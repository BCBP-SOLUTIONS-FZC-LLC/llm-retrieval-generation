"""``IndexGeneration``: the IG-3 / IG-5 / IG-6 state machine (§4.7.3)."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime
from typing import Any
from uuid import UUID

from rag.domain.corpus.errors import GenerationNotVerified, InvalidTransition
from rag.domain.shared.enums import IndexGenerationStatus as S

INITIAL_GENERATION = 1
UNSPECIFIED_PROFILE = "unspecified"
"""Placeholder for ``embedding_profile`` / ``chunker_version`` on generation 1, which C-I1 creates
before Ingestion has reported either (the columns are ``NOT NULL``, IG-2)."""

_EDGES: frozenset[tuple[S, S]] = frozenset(
    {
        (S.REGISTERED, S.BUILDING),
        (S.BUILDING, S.VERIFIED),
        (S.BUILDING, S.FAILED),
        (S.VERIFIED, S.ACTIVE),
        (S.ACTIVE, S.RETIRED),
    }
)
"""Every edge ``reject_generation_transition`` admits. ``failed`` and ``retired`` are terminal."""


def is_allowed_edge(frm: S, to: S) -> bool:
    """Whether the database trigger would admit ``frm`` -> ``to`` for this service's role."""
    return (frm, to) in _EDGES


@dataclass(frozen=True, slots=True)
class IndexGeneration:
    """One ``index_generations`` row; transitions return a new value."""

    collection_id: UUID
    generation: int
    tenant_id: UUID
    embedding_profile: str
    chunker_version: str
    status: S
    registered_at: datetime | None = None
    verified_at: datetime | None = None
    failed_checks: Any = None
    activated_at: datetime | None = None
    retired_at: datetime | None = None

    @classmethod
    def first(
        cls,
        *,
        collection_id: UUID,
        tenant_id: UUID,
        active: bool,
        now: datetime,
        embedding_profile: str = UNSPECIFIED_PROFILE,
        chunker_version: str = UNSPECIFIED_PROFILE,
    ) -> IndexGeneration:
        """Generation 1 from C-I1: ``verified`` (tender), ``active`` (tenant, reference)."""
        return cls(
            collection_id=collection_id,
            generation=INITIAL_GENERATION,
            tenant_id=tenant_id,
            embedding_profile=embedding_profile,
            chunker_version=chunker_version,
            status=S.ACTIVE if active else S.VERIFIED,
            registered_at=now,
            verified_at=now,
            activated_at=now if active else None,
        )

    def _to(self, to: S, **changes: Any) -> IndexGeneration:
        if not is_allowed_edge(self.status, to):
            raise InvalidTransition(f"generation {self.status} -> {to} is not a transition")
        return replace(self, status=to, **changes)

    def begin_build(self) -> IndexGeneration:
        """``registered`` to ``building`` (reserved C-I9)."""
        return self._to(S.BUILDING)

    def verify(self, now: datetime) -> IndexGeneration:
        """``building`` to ``verified`` with ``verified_at`` (reserved C-I3)."""
        return self._to(S.VERIFIED, verified_at=now)

    def fail(self, failed_checks: Any) -> IndexGeneration:
        """``building`` to ``failed`` with ``failed_checks``; terminal (IG-6)."""
        return self._to(S.FAILED, failed_checks=failed_checks)

    def activate(self, now: datetime) -> IndexGeneration:
        """``verified`` to ``active`` only (IG-5); else ``GENERATION_NOT_VERIFIED``."""
        if self.status is not S.VERIFIED:
            raise GenerationNotVerified(
                f"Generation {self.generation} is {self.status}, not verified."
            )
        return self._to(S.ACTIVE, activated_at=now)

    def retire(self, now: datetime) -> IndexGeneration:
        """``active`` to ``retired`` with ``retired_at``; terminal (IG-6)."""
        return self._to(S.RETIRED, retired_at=now)
