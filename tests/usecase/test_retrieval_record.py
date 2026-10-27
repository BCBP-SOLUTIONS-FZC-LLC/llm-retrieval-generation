"""Retrieval-set persistence (§4.4): what is written, in which order, and how it fails."""

from __future__ import annotations

from dataclasses import replace
from decimal import Decimal

import pytest
from tests.support_retrieval import COLL, TENANT, TENDER, Rig, identity, scope

from rag.application.retrieval.dtos import PipelineRequest, SearchCommand
from rag.application.retrieval.search import SearchDocuments
from rag.application.shared.gateway import GatewayUnavailable
from rag.domain.shared.enums import GrantLevel, RetrievalVerdict
from rag.domain.shared.errors import DomainError, ErrorCode

REQ = PipelineRequest("crane erection access route", section_type="method_statement")


async def test_search_records_the_set_with_verdict_filters_markers_and_chunks() -> None:
    """R-1 writes one set, artefact_id null, carrying what a replay needs (RS-3, RS-7)."""
    rig = Rig(refiner=True)
    rig.gw.rerank_error = GatewayUnavailable()
    uc = SearchDocuments(rig.pipeline, rig.recorder)
    result = await uc.execute(identity(), SearchCommand(REQ))
    (view,) = rig.sets.inserted
    s = view.retrieval_set
    assert result.retrieval_set_id == s.id and s.artefact_id is None
    assert (s.tenant_id, s.tender_id) == (TENANT, TENDER)
    assert s.verdict is RetrievalVerdict.SUFFICIENT and s.verdict_reason is None
    assert s.scope_resolution_hash == "h" * 64
    assert (s.grant_level_applied, s.grant_level_clamped) == (GrantLevel.GENERATE, False)
    assert (
        s.filter_set["is_current"] is True
        and s.filter_set["collection_ids"] == []
        and s.filter_set["resolved_collection_ids"] == [str(COLL)]
    )
    assert s.filter_set["document_roles"] == []  # the pack's preference
    assert s.filter_set["refiner_roles"] == ["specification"]  # recorded apart (RET-2)
    assert s.index_generations == {str(COLL): 1}
    assert s.profile_source == "section_type" and s.applied_profile["rerank_out"] == 5
    assert s.reranker_ran is False and s.degradation_markers == [
        {"stage": "rerank", "reason": "unavailable", "applied_fallback": "fused_order"}
    ]
    assert (s.domain_profile_version, s.language_pack_version) == ("dp-1", "lp-1")
    assert s.budget_allocation["evidence_tokens"] > 0
    assert [c.rank for c in view.chunks] == list(range(1, len(view.chunks) + 1))
    assert all(c.tenant_id == TENANT and c.excerpt for c in view.chunks)
    assert isinstance(view.chunks[0].collection_weight, Decimal)
    assert view.chunks[0].rerank_score is None  # fused order served


async def test_the_refinement_row_is_written_after_its_set_and_points_at_it() -> None:
    """The row follows the set in the same transaction (query_refinements.retrieval_set_id)."""
    rig = Rig(refiner=True)
    out = await rig.pipeline.run(identity(), REQ)
    recorded = await rig.recorder.record(identity(), out)
    (row,) = rig.refinements.rows
    assert row.retrieval_set_id == recorded.retrieval_set_id
    assert rig.sets.inserted[0].retrieval_set.query_refinement_id == row.id
    assert row.principal_hash != str(identity().principal_id) and len(row.principal_hash) == 64
    assert rig.tx.runs == 1  # one transaction for set(s) and refinement


async def test_a_widened_retry_records_both_sets_and_the_second_names_the_first() -> None:
    """RS-6; the refinement belongs to the set that first used it."""
    rig = Rig(refiner=True)
    rig.gw.rerank_scores = lambda p: [0.05] * len(p)
    out = await rig.pipeline.run(identity(), REQ)
    recorded = await rig.recorder.record(identity(), out)
    first, second = (v.retrieval_set for v in rig.sets.inserted)
    assert first.widened_retry_of is None and first.verdict is RetrievalVerdict.INSUFFICIENT
    assert first.verdict_reason  # RS-5
    assert second.widened_retry_of == first.id == recorded.widened_retry_of
    assert recorded.retrieval_set_id == second.id
    assert rig.refinements.rows[0].retrieval_set_id == first.id


async def test_a_cache_hit_reuses_the_refinement_and_writes_a_fresh_set() -> None:
    """Each request has its own set id (API-9); the refinement row is not duplicated."""
    rig = Rig(refiner=True, cache=True)
    await rig.recorder.record(identity(), await rig.pipeline.run(identity(), REQ))
    hit = await rig.pipeline.run(identity(), REQ)
    assert hit.cache_hit
    second = await rig.recorder.record(identity(), hit)
    assert len(rig.sets.inserted) == 2 and len(rig.refinements.rows) == 1
    assert rig.sets.inserted[1].retrieval_set.query_refinement_id == rig.refinements.rows[0].id
    assert second.retrieval_set_id != rig.sets.inserted[0].retrieval_set.id


async def test_a_failed_write_is_503_artefact_store_unwritable() -> None:
    """§5.7.1: the response carries an id that must resolve, so a lost write is an error."""
    rig = Rig()
    rig.tx.fail = RuntimeError("deadlock")
    with pytest.raises(DomainError) as e:
        await SearchDocuments(rig.pipeline, rig.recorder).execute(identity(), SearchCommand(REQ))
    assert e.value.code is ErrorCode.ARTEFACT_STORE_UNWRITABLE
    rig.tx.fail = DomainError(ErrorCode.SCOPE_STORE_UNAVAILABLE)
    with pytest.raises(DomainError) as e2:
        await rig.recorder.record(identity(), await rig.pipeline.run(identity(), REQ))
    assert e2.value.code is ErrorCode.SCOPE_STORE_UNAVAILABLE  # domain errors pass through


async def test_nothing_is_recorded_when_retrieval_fails() -> None:
    """MD-14: a failed verdict returns no id and writes nothing."""
    from rag.application.retrieval.ports import SearchHalfError

    rig = Rig()
    rig.dense.error = rig.lexical.error = SearchHalfError()
    with pytest.raises(DomainError):
        await SearchDocuments(rig.pipeline, rig.recorder).execute(identity(), SearchCommand(REQ))
    assert rig.sets.inserted == [] and rig.tx.runs == 0


async def test_clamped_scope_is_recorded() -> None:
    """RS-2 / SCOPE-8: the applied level and the clamp flag are the set's."""
    rig = Rig()
    s = scope(level=GrantLevel.ANSWER)
    rig.provider.result = replace(s, clamped=True)
    await rig.recorder.record(identity(), await rig.pipeline.run(identity(), REQ))
    stored = rig.sets.inserted[0].retrieval_set
    assert (stored.grant_level_applied, stored.grant_level_clamped) == (GrantLevel.ANSWER, True)
