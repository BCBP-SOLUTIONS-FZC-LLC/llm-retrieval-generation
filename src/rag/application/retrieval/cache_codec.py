"""JSON form of a ``RetrievalOutcome`` for the retrieval-set cache (§11.1).

Only the final outcome is cached; the widened-retry history is not. Chunk text is cached whole
(Valkey, tenant-prefixed key, 15 minutes), because a generation route reuses the outcome.
"""

from __future__ import annotations

import json
from dataclasses import asdict
from decimal import Decimal
from typing import Any
from uuid import UUID

from rag.application.retrieval.dtos import RetrievalOutcome
from rag.domain.generation.refinement import QueryRefinement
from rag.domain.packs.profile import ProfileSource
from rag.domain.retrieval.budget import ContextBudget
from rag.domain.retrieval.candidate import Candidate
from rag.domain.retrieval.plan import DegradationMarker, RetrievalPlan, StructuralFilters
from rag.domain.retrieval.scope import ResolvedScope
from rag.domain.shared.enums import RefinerOutcome, RetrievalVerdict


def _chunk(c: Candidate) -> dict[str, Any]:
    d = asdict(c)
    d.update(document_id=str(c.document_id), collection_id=str(c.collection_id))
    d["weight"] = str(c.weight)
    return d


def _refinement(r: QueryRefinement) -> dict[str, Any]:
    d = asdict(r)
    d.update(id=str(r.id), tenant_id=str(r.tenant_id), outcome=r.outcome.value)
    d["retrieval_set_id"] = None
    return d


def outcome_to_json(o: RetrievalOutcome) -> str:
    """Serialise the final outcome (the scope is the reader's, not the cache's)."""
    p = o.plan
    return json.dumps(
        {
            "verdict": o.verdict.value,
            "reason": o.verdict_reason,
            "chunks": [_chunk(c) for c in o.chunks],
            "markers": [m.as_dict() for m in o.markers],
            "plan": {
                "queries": list(p.queries),
                "roles": list(p.filters.document_roles),
                "refiner_roles": list(p.refiner_roles),
                "language": p.filters.language,
                "depth": p.candidate_depth,
                "rerank_in": p.rerank_in,
                "rerank_out": p.rerank_out,
                "diversity": p.diversity_bound,
                "source": p.profile_source.value,
                "provisional": p.values_provisional,
                "budget": p.budget.as_dict(),
                "extra": p.extra,
            },
            "reranker_ran": o.reranker_ran,
            "dpv": o.domain_profile_version,
            "lpv": o.language_pack_version,
            "refinement": None if o.refinement is None else _refinement(o.refinement),
            "widened": o.widened_retry,
        }
    )


def outcome_from_json(raw: str, scope: ResolvedScope) -> RetrievalOutcome:
    """Rebuild under the *current* scope. Raises ``ValueError``/``KeyError`` on a bad entry."""
    d = json.loads(raw)
    p = d["plan"]
    refinement = None
    if d["refinement"] is not None:
        r = d["refinement"]
        refinement = QueryRefinement(
            id=UUID(r["id"]),
            tenant_id=UUID(r["tenant_id"]),
            principal_hash=r["principal_hash"],
            original_query=r["original_query"],
            pack_version=r["pack_version"],
            refiner_version=r["refiner_version"],
            latency_ms=r["latency_ms"],
            outcome=RefinerOutcome(r["outcome"]),
            refined_query=r["refined_query"],
            intent=r["intent"],
            exact_terms=tuple(r["exact_terms"]),
            preferred_roles=tuple(r["preferred_roles"]),
            section_type=r["section_type"],
        )
    return RetrievalOutcome(
        scope=scope,
        verdict=RetrievalVerdict(d["verdict"]),
        verdict_reason=d["reason"],
        chunks=tuple(
            Candidate(
                **{
                    **c,
                    "document_id": UUID(c["document_id"]),
                    "collection_id": UUID(c["collection_id"]),
                    "weight": Decimal(c["weight"]),
                }
            )
            for c in d["chunks"]
        ),
        markers=tuple(DegradationMarker(**m) for m in d["markers"]),
        plan=RetrievalPlan(
            queries=tuple(p["queries"]),
            filters=StructuralFilters(tuple(p["roles"]), p["language"]),
            candidate_depth=p["depth"],
            rerank_in=p["rerank_in"],
            rerank_out=p["rerank_out"],
            diversity_bound=p["diversity"],
            profile_source=ProfileSource(p["source"]),
            values_provisional=p["provisional"],
            budget=ContextBudget(**p["budget"]),
            extra=p["extra"],
            refiner_roles=tuple(p["refiner_roles"]),
        ),
        reranker_ran=d["reranker_ran"],
        domain_profile_version=d["dpv"],
        language_pack_version=d["lpv"],
        refinement=refinement,
        refinement_persisted=refinement is not None,
        widened_retry=d["widened"],
        cache_hit=True,
    )
