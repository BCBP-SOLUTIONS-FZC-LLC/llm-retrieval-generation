"""``query_refinements`` (LLD §4.6.1): one row per refiner call, whatever the outcome."""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from rag.domain.shared.enums import RefinerOutcome


@dataclass(frozen=True, slots=True)
class QueryRefinement:
    """What the Query Refiner returned (§6.5), as stored for the golden-set dataset (REF-1).

    ``refined_query`` and the lists are empty where the refiner degraded. The original query
    is always retained (MD-33). ``principal_hash`` is a per-tenant salted one-way hash (QR-3).
    """

    id: UUID
    tenant_id: UUID
    principal_hash: str
    original_query: str
    pack_version: str
    refiner_version: str
    latency_ms: int
    outcome: RefinerOutcome
    refined_query: str | None = None
    intent: str | None = None
    exact_terms: tuple[str, ...] = ()
    preferred_roles: tuple[str, ...] = ()
    section_type: str | None = None
    retrieval_set_id: UUID | None = None
