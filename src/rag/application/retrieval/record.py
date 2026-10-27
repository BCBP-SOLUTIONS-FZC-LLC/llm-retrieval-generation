"""Retrieval-set persistence (§4.4, §13.1): verdict, filters, markers, chunks, refinement."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from uuid import UUID, uuid4

from rag.application.retrieval.dtos import RetrievalOutcome
from rag.application.shared.ports import TransactionRunner, TxHandle
from rag.domain.generation.repositories import QueryRefinementRepository, RetrievalSetRepository
from rag.domain.generation.retrieval import RetrievalChunk, RetrievalSet, RetrievalSetView
from rag.domain.shared.errors import DomainError, ErrorCode
from rag.domain.shared.identity import RequestIdentity


@dataclass(frozen=True, slots=True)
class RecordedSet:
    """Ids of what was written: the final set, and the first attempt where a retry ran."""

    retrieval_set_id: UUID
    widened_retry_of: UUID | None


def _view(
    set_id: UUID,
    identity: RequestIdentity,
    outcome: RetrievalOutcome,
    *,
    tender_id: UUID | None,
    artefact_id: UUID | None,
    widened_retry_of: UUID | None,
    refinement_id: UUID | None,
) -> RetrievalSetView:
    scope, plan = outcome.scope, outcome.plan
    filters = scope.filter_set()
    # R-5 re-resolves what the caller named, not every collection that happened to be in scope
    filters["resolved_collection_ids"] = filters["collection_ids"]
    filters["collection_ids"] = plan.extra.get("named_collection_ids", [])
    filters["document_roles"] = list(plan.filters.document_roles)
    filters["refiner_roles"] = list(plan.refiner_roles)
    filters["language"] = plan.filters.language
    stored = RetrievalSet(
        id=set_id,
        tenant_id=identity.tenant_id,
        tender_id=tender_id,
        artefact_id=artefact_id,
        verdict=outcome.verdict,
        verdict_reason=outcome.verdict_reason,
        scope_resolution_hash=scope.resolution_hash,
        grant_level_applied=scope.level,
        grant_level_clamped=scope.clamped,
        filter_set=filters,
        index_generations={str(c.id): c.index_generation for c in scope.collections},
        profile_source=plan.profile_source.value,
        applied_profile=plan.applied_profile(),
        values_provisional=plan.values_provisional,
        reranker_ran=outcome.reranker_ran,
        budget_allocation=plan.budget.as_dict(),
        query_refinement_id=refinement_id,
        widened_retry_of=widened_retry_of,
        domain_profile_version=outcome.domain_profile_version,
        language_pack_version=outcome.language_pack_version,
        degradation_markers=[m.as_dict() for m in outcome.markers],
    )
    chunks = tuple(
        RetrievalChunk(
            retrieval_set_id=set_id,
            tenant_id=identity.tenant_id,  # the caller's tenant, reference chunks included (RLS)
            rank=rank,
            chunk_id=c.chunk_id,
            document_id=c.document_id,
            document_version=c.document_version,
            collection_id=c.collection_id,
            collection_weight=c.weight,
            fused_score=Decimal(repr(c.fused_score)),
            rerank_score=None if c.rerank_score is None else Decimal(repr(c.rerank_score)),
            source_half=c.source_half,
            document_title=c.document_title,
            document_role=c.document_role,
            language=c.language,
            excerpt=c.excerpt,
            parent_chunk_id=c.parent_chunk_id,
            heading_path=c.heading_path,
            clause_ref=c.clause_ref,
        )
        for rank, c in enumerate(outcome.chunks, start=1)
    )
    return RetrievalSetView(stored, chunks, ())


class RecordRetrievalSet:
    """Writes the set(s) of an outcome and its refinement row in one ``llm_generation`` tx.

    On a widened retry both attempts are recorded and the second names the first (RS-6). A
    failure to write is ``503 ARTEFACT_STORE_UNWRITABLE``: the response carries chunks and an id
    that must resolve (§5.7.1). Reusable by the generation routes with an ``artefact_id``.
    """

    def __init__(
        self,
        tx: TransactionRunner,
        sets: RetrievalSetRepository,
        refinements: QueryRefinementRepository,
    ) -> None:
        self._tx, self._sets, self._refinements = tx, sets, refinements

    async def record(
        self,
        identity: RequestIdentity,
        outcome: RetrievalOutcome,
        *,
        tender_id: UUID | None = None,
        artefact_id: UUID | None = None,
    ) -> RecordedSet:
        """Persist and return the final set's id."""
        scope_tender = outcome.scope.tender_ids
        tender = tender_id or (scope_tender[0] if len(scope_tender) == 1 else None)
        refinement = outcome.refinement
        first = outcome.first_attempt
        first_id = uuid4() if first else None
        final_id = uuid4()
        anchor = first_id or final_id  # the refinement belongs to the set that first used it

        async def work(tx: TxHandle) -> None:
            ref_id = refinement.id if refinement else None
            if first is not None and first_id is not None:
                await self._sets.insert(
                    tx,
                    _view(
                        first_id,
                        identity,
                        first,
                        tender_id=tender,
                        artefact_id=artefact_id,
                        widened_retry_of=None,
                        refinement_id=ref_id,
                    ),
                )
            await self._sets.insert(
                tx,
                _view(
                    final_id,
                    identity,
                    outcome,
                    tender_id=tender,
                    artefact_id=artefact_id,
                    widened_retry_of=first_id,
                    refinement_id=ref_id,
                ),
            )
            if refinement is not None and not outcome.refinement_persisted:
                from dataclasses import replace

                await self._refinements.insert(tx, replace(refinement, retrieval_set_id=anchor))

        try:
            await self._tx.run_in_tx(work)
        except DomainError:
            raise
        except Exception as exc:
            raise DomainError(
                ErrorCode.ARTEFACT_STORE_UNWRITABLE, "The retrieval set could not be recorded."
            ) from exc
        return RecordedSet(final_id, first_id)
