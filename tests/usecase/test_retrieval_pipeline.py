"""The retrieval pipeline over fakes: stages, degradation (RET-1), widened retry, cache, trim."""

from __future__ import annotations

import pytest
from tests.support_retrieval import (
    COLL,
    COLL_B,
    FakePacks,
    Rig,
    bundle,
    chunk,
    identity,
    scope,
    section_profile,
)

from rag.application.retrieval.dtos import PipelineRequest
from rag.application.retrieval.pipeline import PipelineConfig
from rag.application.retrieval.ports import SearchHalfError
from rag.application.shared.gateway import (
    GatewayQuotaRefused,
    GatewayResidencyRefused,
    GatewayUnavailable,
)
from rag.domain.retrieval.sufficiency import RequiredInputRule
from rag.domain.shared.enums import GrantLevel, RefinerOutcome, RetrievalVerdict
from rag.domain.shared.errors import DomainError, ErrorCode

REQ = PipelineRequest("crane erection access route", section_type="method_statement")


def marks(outcome) -> list[tuple[str, str, str]]:  # type: ignore[no-untyped-def]
    """``(stage, reason, fallback)`` of every marker."""
    return [(m.stage, m.reason, m.applied_fallback) for m in outcome.markers]


async def test_happy_path_runs_every_stage_and_is_not_degraded() -> None:
    """Stages 0-9: sufficient, reranked, labelled halves, excerpts set, scope asked at search."""
    rig = Rig()
    out = await rig.pipeline.run(identity(), REQ)
    assert out.verdict is RetrievalVerdict.SUFFICIENT and out.verdict_reason is None
    assert out.reranker_ran and not out.degraded and out.markers == ()
    assert [c.chunk_id for c in out.chunks][0] == "v2:2"  # in both halves
    assert out.chunks[0].source_half == "both" and out.chunks[0].excerpt
    assert [c.rerank_score for c in out.chunks] == sorted(
        (c.rerank_score for c in out.chunks), reverse=True
    )
    assert rig.provider.requests[0][1] is GrantLevel.SEARCH
    stages = [e[1] for e in rig.obs.of("stage")]
    assert stages == ["scope", "pack", "refine", "plan", "search", "fusion", "rerank", "assembly"]
    assert rig.obs.of("verdict") == [("verdict", "sufficient", "method_statement")]
    assert out.plan.applied_profile() == {
        "source": "section_type",
        "values_provisional": False,
        "candidate_depth": 24,
        "rerank_in": 10,
        "rerank_out": 5,
    }


async def test_unknown_section_type_is_never_a_metric_label() -> None:
    """OBS-1: client input is not a label; only a pack-declared section type is."""
    rig = Rig()
    await rig.pipeline.run(identity(), PipelineRequest("q", section_type="x" * 200))
    assert rig.obs.of("verdict")[0][2] is None


async def test_limit_is_clamped_to_rerank_out() -> None:
    """§5.7.1: a caller asking for more than the profile reranks gets rerank_out, echoed."""
    rig = Rig()
    assert len((await rig.pipeline.run(identity(), PipelineRequest("q", limit=99))).chunks) <= 5
    two = await rig.pipeline.run(identity(), PipelineRequest("q", limit=2))
    assert len(two.chunks) == 2


async def test_scope_failure_stops_the_request_before_anything_else_runs() -> None:
    """Stage 0 fails the request (404 here) and records the empty-scope metric."""
    rig = Rig()
    rig.provider.error = DomainError(ErrorCode.COLLECTION_NOT_FOUND)
    with pytest.raises(DomainError) as e:
        await rig.pipeline.run(identity(), REQ)
    assert e.value.code is ErrorCode.COLLECTION_NOT_FOUND
    assert rig.obs.of("scope_empty") and rig.packs.calls == 0 and not rig.dense.calls


async def test_pack_outage_falls_to_the_platform_default_with_a_marker() -> None:
    """LD-14: only where Tender is unavailable; every value provisional (RET-4)."""
    rig = Rig(packs=FakePacks(down=True))
    out = await rig.pipeline.run(identity(), REQ)
    assert marks(out)[0] == ("pack", "tender_unavailable", "platform_default")
    assert out.plan.profile_source.value == "platform_default" and out.plan.values_provisional
    assert out.domain_profile_version == "platform-default-1"
    assert out.degraded


async def test_all_collections_empty_is_an_insufficient_verdict_not_an_error() -> None:
    """SCOPE-12: no search is issued, the reason says why, and there is no widened retry."""
    rig = Rig()
    rig.provider.result = scope(empty=(COLL,))
    out = await rig.pipeline.run(identity(), REQ)
    assert out.verdict is RetrievalVerdict.INSUFFICIENT
    assert out.verdict_reason == "no_searchable_collection"
    assert not rig.dense.calls and not rig.lexical.calls and not rig.obs.of("widened")


# --- the refiner (§6.5) -----------------------------------------------------------------------


async def test_refiner_off_means_no_row_no_marker_and_no_call() -> None:
    """REF-2: flag-off is not a degradation."""
    rig = Rig(refiner=False)
    out = await rig.pipeline.run(identity(), REQ)
    assert out.refinement is None and not rig.gw.completions and not out.degraded


async def test_the_original_query_is_always_searched_beside_the_refined_one() -> None:
    """RET-2 / §6.5: separate lexical lists for original and refined; exact terms as phrases."""
    rig = Rig(refiner=True)
    out = await rig.pipeline.run(identity(), REQ)
    queries = [c[0] for c in rig.lexical.calls]
    assert queries[0] == REQ.query
    assert "crane erection access route" in queries and '"crane"' in queries
    assert rig.gw.embeds[0] == ["crane erection access route"]
    assert out.refinement is not None and out.refinement.outcome is RefinerOutcome.SUCCESS
    assert out.refinement.refiner_version.startswith("qr-")
    assert out.plan.filters.document_roles == () and out.plan.refiner_roles == ("specification",)


@pytest.mark.parametrize(
    ("setup", "reason"),
    [
        ("delay", "deadline_exceeded"),
        ("quota", "quota_refused"),
        ("residency", "residency_refused"),
        ("down", "unavailable"),
        ("garbage", "invalid_output"),
        ("bad_intent", "invalid_output"),
    ],
)
async def test_refiner_failure_degrades_to_the_raw_query(setup: str, reason: str) -> None:
    """RET-1: a marker, a degraded row, and the raw query searched."""
    rig = Rig(refiner=True)
    if setup == "delay":
        rig.gw.complete_delay = 1.0
    elif setup == "quota":
        rig.gw.complete_error = GatewayQuotaRefused()
    elif setup == "residency":
        rig.gw.complete_error = GatewayResidencyRefused()
    elif setup == "down":
        rig.gw.complete_error = GatewayUnavailable()
    elif setup == "garbage":
        rig.gw.complete_text = "not json"
    else:
        rig.gw.complete_text = '{"intent":"mystery","refined_query":"x"}'
    out = await rig.pipeline.run(identity(), REQ)
    assert ("refine", reason, "raw_query") in marks(out)
    assert out.refinement is not None and out.refinement.outcome is RefinerOutcome.DEGRADED
    assert out.refinement.refined_query is None
    assert [c[0] for c in rig.lexical.calls] == [REQ.query]
    assert rig.obs.of("marker")[0][1:] == ("refine", reason)


async def test_refined_query_cache_short_circuits_a_repeat_and_follows_the_refiner_version() -> (
    None
):
    """§11.1: second identical request makes no Gateway call; a bad entry is a miss."""
    rig = Rig(refiner=True, cache=True)
    kwargs = dict(
        tenant_id=identity().tenant_id,
        principal_id=None,
        query="Crane  Access",
        bundle=bundle(),
        section_type=None,
        ctx=__import__(
            "rag.application.shared.gateway", fromlist=["GatewayContext"]
        ).GatewayContext(),
    )
    first = await rig.refiner.refine(**kwargs)  # type: ignore[arg-type]
    second = await rig.refiner.refine(**kwargs)  # type: ignore[arg-type]
    assert len(rig.gw.completions) == 1
    assert first and second and second.refinement.refined_query == first.refinement.refined_query
    key = next(k for k in rig.kv.data if ":qref:" in k)  # type: ignore[union-attr]
    assert key.startswith(f"{identity().tenant_id}:qref:crane access:-:dp-1")
    rig.kv.data[key] = "{bad"  # type: ignore[union-attr]
    await rig.refiner.refine(**kwargs)  # type: ignore[arg-type]
    assert len(rig.gw.completions) == 2


# --- search-half and rerank degradation ------------------------------------------------------


@pytest.mark.parametrize(
    ("error", "stage", "reason"),
    [
        (GatewayQuotaRefused(), "embed", "quota_refused"),
        (GatewayResidencyRefused(), "embed", "residency_refused"),
        (GatewayUnavailable(), "embed", "unavailable"),
    ],
)
async def test_embed_failure_falls_to_lexical_only(error, stage, reason) -> None:  # type: ignore[no-untyped-def]
    """§14.3 / LD-99: lexical-only with a marker; a refusal is a degradation, not an error."""
    rig = Rig()
    rig.gw.embed_error = error
    out = await rig.pipeline.run(identity(), REQ)
    assert (stage, reason, "lexical_only") in marks(out)
    assert {c.source_half for c in out.chunks} == {"lexical"}


async def test_dense_failure_falls_to_lexical_and_lexical_failure_to_dense() -> None:
    """Either half alone serves, each with its marker; the other half's chunks come through."""
    rig = Rig()
    rig.dense.error = SearchHalfError()
    out = await rig.pipeline.run(identity(), REQ)
    assert ("dense_search", "unavailable", "lexical_only") in marks(out)
    rig2 = Rig()
    rig2.lexical.error = SearchHalfError()
    out2 = await rig2.pipeline.run(identity(), REQ)
    assert ("lexical_search", "unavailable", "dense_only") in marks(out2)
    assert {c.source_half for c in out2.chunks} == {"dense"}


async def test_both_halves_down_is_retrieval_failed_and_never_a_verdict() -> None:
    """MD-14: a failed retrieval is a 503, not a body."""
    rig = Rig()
    rig.dense.error = SearchHalfError()
    rig.lexical.error = SearchHalfError()
    with pytest.raises(DomainError) as e:
        await rig.pipeline.run(identity(), REQ)
    assert e.value.code is ErrorCode.RETRIEVAL_FAILED
    rig2 = Rig()
    rig2.gw.embed_error = GatewayUnavailable()
    rig2.lexical.error = SearchHalfError()
    with pytest.raises(DomainError):
        await rig2.pipeline.run(identity(), REQ)


async def test_rerank_deadline_keeps_the_fused_order_and_never_retries() -> None:
    """§6.7: 90 ms deadline, no retry, a marker; the fused order serves."""
    rig = Rig(config=PipelineConfig(rerank_deadline_seconds=0.05))
    rig.gw.rerank_delay = 1.0
    out = await rig.pipeline.run(identity(), REQ)
    assert ("rerank", "deadline_exceeded", "fused_order") in marks(out)
    assert not out.reranker_ran and len(rig.gw.reranks) == 1
    assert all(c.rerank_score is None for c in out.chunks)
    assert rig.obs.of("rerank_skipped") == [("rerank_skipped", "deadline_exceeded")]
    assert out.verdict is RetrievalVerdict.SUFFICIENT  # judged on count when not reranked


@pytest.mark.parametrize(
    ("error", "reason"),
    [(GatewayQuotaRefused(), "quota_refused"), (GatewayResidencyRefused(), "residency_refused")],
)
async def test_rerank_refusals_are_degradations(error, reason) -> None:  # type: ignore[no-untyped-def]
    """LD-99: the reason distinguishes a refusal from a deadline (the alert excludes them)."""
    rig = Rig()
    rig.gw.rerank_error = error
    out = await rig.pipeline.run(identity(), REQ)
    assert ("rerank", reason, "fused_order") in marks(out)


async def test_a_reranker_returning_the_wrong_number_of_scores_is_a_failure_not_a_guess() -> None:
    """Defensive: mismatched scores degrade to fused order."""
    rig = Rig()
    rig.gw.rerank_scores = lambda p: [0.9]
    out = await rig.pipeline.run(identity(), REQ)
    assert ("rerank", "unavailable", "fused_order") in marks(out)


# --- sufficiency and the widened retry (D-16) -------------------------------------------------


async def test_insufficient_triggers_exactly_one_widened_retry_that_never_widens_scope() -> None:
    """Deeper, role preference relaxed, same scope; the first attempt is kept for the record."""
    rig = Rig()
    rig.packs.bundle = bundle(section_profile(preferred_roles=("specification",)))
    rig.gw.rerank_scores = lambda p: [0.05] * len(p)
    out = await rig.pipeline.run(identity(), REQ)
    assert out.verdict is RetrievalVerdict.INSUFFICIENT and out.widened_retry
    assert out.first_attempt is not None and out.first_attempt.verdict_reason
    assert len(rig.dense.calls) == 2  # one retry, never a third attempt
    (d1, f1), (d2, f2) = rig.dense.calls
    assert d2 == d1 * 2 and f1.document_roles == ("specification",) and f2.document_roles == ()
    assert len(rig.provider.requests) == 1  # scope resolved once: SCOPE-6
    assert rig.obs.of("widened") == [("widened",)]


async def test_a_retry_that_succeeds_returns_sufficient_with_the_retry_flag() -> None:
    """The retry's own verdict is the answer."""
    rig = Rig()
    rig.gw.rerank_scores = lambda p: [0.05] * len(p)
    calls = {"n": 0}

    def scores(p: list[str]) -> list[float]:
        calls["n"] += 1
        return [0.05] * len(p) if calls["n"] == 1 else [0.9] * len(p)

    rig.gw.rerank_scores = scores
    out = await rig.pipeline.run(identity(), REQ)
    assert out.verdict is RetrievalVerdict.SUFFICIENT and out.widened_retry
    assert out.first_attempt is not None
    assert out.first_attempt.verdict is RetrievalVerdict.INSUFFICIENT


# --- budget and trim --------------------------------------------------------------------------


async def test_user_input_over_ten_percent_of_the_window_is_413() -> None:
    """§6.8: rejected before any retrieval, never trimmed."""
    rig = Rig(config=PipelineConfig(context_window_tokens=100))
    with pytest.raises(DomainError) as e:
        await rig.pipeline.run(identity(), PipelineRequest("long query " * 20))
    assert e.value.code is ErrorCode.USER_INPUT_OVER_BUDGET and not rig.dense.calls


async def test_a_trim_that_would_drop_required_evidence_returns_insufficient_untrimmed() -> None:
    """§6.8: Insufficient, nothing silently dropped."""
    rig = Rig(config=PipelineConfig(context_window_tokens=300, mean_chunk_tokens=10))
    rig.gw.token_per_char = 1.0
    rig.packs.bundle = bundle(
        section_profile(sufficiency=rig.packs.bundle.domain_profile.default_profile.sufficiency),
        required_inputs={"method_statement": RequiredInputRule(("drawing",))},
    )
    drawing = chunk(9, role="drawing", text="DRAWING sheet with a unique heading text for A-9.")
    rig.dense.hits = [chunk(1), chunk(2), chunk(3), drawing]
    rig.lexical.hits = []
    rig.gw.rerank_scores = lambda p: [0.1 if "DRAWING" in t else 0.9 for t in p]
    out = await rig.pipeline.run(
        identity(), PipelineRequest("crane erection access route", section_type="method_statement")
    )
    assert out.verdict_reason == "trim_would_drop_required_evidence"
    assert any(c.document_role == "drawing" for c in out.chunks)


async def test_enforced_required_inputs_refuse_a_blocking_gap_with_422() -> None:
    """R-3/A-6 behaviour (§6.9): REQUIRED_INPUT_MISSING carries the gap; a pass proceeds."""
    rig = Rig()
    rig.packs.bundle = bundle(required_inputs={"method_statement": RequiredInputRule(("drawing",))})
    req = PipelineRequest("q", section_type="method_statement", enforce_required_inputs=True)
    with pytest.raises(DomainError) as e:
        await rig.pipeline.run(identity(), req)
    assert e.value.code is ErrorCode.REQUIRED_INPUT_MISSING
    assert e.value.extensions["missingRoles"] == ["drawing"]
    rig.inspector.counts[("role", "drawing")] = 3
    assert (await rig.pipeline.run(identity(), req)).verdict is RetrievalVerdict.SUFFICIENT
    rig.inspector.error = SearchHalfError()
    with pytest.raises(DomainError) as e2:
        await rig.pipeline.run(identity(), req)
    assert e2.value.code is ErrorCode.RETRIEVAL_FAILED  # never a guess either way


# --- the retrieval-set cache (§11) ------------------------------------------------------------


async def test_cache_hit_serves_without_searching_and_epoch_bump_misses() -> None:
    """§11.3: keyed with the purge epoch; a bump is a miss (O(1) invalidation)."""
    rig = Rig(cache=True)
    first = await rig.pipeline.run(identity(), REQ)
    second = await rig.pipeline.run(identity(), REQ)
    assert second.cache_hit and not first.cache_hit and len(rig.dense.calls) == 1
    assert [c.chunk_id for c in second.chunks] == [c.chunk_id for c in first.chunks]
    assert second.chunks[0].text == first.chunks[0].text  # a generation route reuses it
    rig.provider.result = scope(epoch=1)  # DocumentPurged bumped the collection's epoch
    third = await rig.pipeline.run(identity(), REQ)
    assert not third.cache_hit and len(rig.dense.calls) == 2
    assert rig.obs.of("cache") == [("cache", False), ("cache", True), ("cache", False)]


async def test_cache_key_is_tenant_prefixed_and_scope_specific() -> None:
    """§4.10 / §11.1: tenant-prefixed, and a different resolved scope is a different key."""
    rig = Rig(cache=True)
    await rig.pipeline.run(identity(), REQ)
    key = next(k for k in rig.kv.data if ":rset:" in k)  # type: ignore[union-attr]
    assert key.startswith(f"{identity().tenant_id}:rset:{'h' * 20}:")
    rig.provider.result = scope(ids=(COLL, COLL_B))
    await rig.pipeline.run(identity(), REQ)
    assert len([k for k in rig.kv.data if ":rset:" in k]) == 2  # type: ignore[union-attr]


async def test_degraded_results_are_not_cached() -> None:
    """A fallback must not stick for 15 minutes."""
    rig = Rig(cache=True)
    rig.gw.embed_error = GatewayUnavailable()
    await rig.pipeline.run(identity(), REQ)
    rig.gw.embed_error = None
    again = await rig.pipeline.run(identity(), REQ)
    assert not again.cache_hit and not again.degraded


async def test_embedding_cache_is_used_for_a_repeated_query() -> None:
    """§11.1 ``qemb``: the second embed of the same text never reaches the Gateway."""
    rig = Rig(cache=True)
    await rig.pipeline.run(identity(), REQ)
    rig.provider.result = scope(epoch=5)  # bypass the retrieval-set cache
    await rig.pipeline.run(identity(), REQ)
    assert len(rig.gw.embeds) == 1
    assert any(":qemb:dp-1:embed-default:" in k for k in rig.kv.data)  # type: ignore[union-attr]


async def test_refiner_advised_roles_never_narrow_the_original_queries_results() -> None:
    """RET-2 / D-34: a wrong refiner guess cannot remove a path the raw query would find."""
    rig = Rig(refiner=True)
    req = PipelineRequest("crane access", section_type="method_statement")
    out = await rig.pipeline.run(identity(), req)
    by_query = {q: f.document_roles for q, _, f in rig.lexical.calls}
    assert by_query["crane access"] == ()  # the original: no refiner role
    assert by_query["crane erection access route"] == ("specification",)  # refined
    assert by_query['"crane"'] == ("specification",)  # exact terms
    assert rig.dense.calls[0][1].document_roles == (
        "specification",
    )  # dense embeds the refined text
    assert out.plan.filters.document_roles == ()  # the pack's own preference is what is shared


async def test_the_widened_retry_drops_the_refiner_roles_too() -> None:
    """§6.5: preferred roles are the first thing relaxed; both sets go."""
    rig = Rig(refiner=True)
    rig.packs.bundle = bundle(section_profile(preferred_roles=("drawing",)))
    rig.gw.rerank_scores = lambda p: [0.05] * len(p)
    out = await rig.pipeline.run(identity(), REQ)
    assert (
        out.widened_retry and out.plan.filters.document_roles == () and out.plan.refiner_roles == ()
    )
    first = out.first_attempt
    assert first is not None
    assert first.plan.filters.document_roles == ("drawing",)
    assert first.plan.refiner_roles == ("specification",)
