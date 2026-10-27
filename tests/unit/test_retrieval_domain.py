"""RRF fusion, budget and trim, sufficiency, required-input rules, text helpers (LLD §6.7-6.9)."""

from __future__ import annotations

from decimal import Decimal
from uuid import uuid4

import pytest
from tests.support_retrieval import chunk

from rag.domain.retrieval.budget import (
    Protection,
    candidate_depth,
    plan_budget,
    trim_to_budget,
)
from rag.domain.retrieval.fusion import RankedList, rank_offset, rrf_fuse
from rag.domain.retrieval.sufficiency import (
    RequiredInputRule,
    SufficiencyThreshold,
    evaluate_required_inputs,
    judge,
)
from rag.domain.retrieval.text import normalise_query, snap_excerpt
from rag.domain.shared.enums import RetrievalVerdict
from rag.domain.shared.errors import DomainError, ErrorCode

# --- RRF (LD-24) ------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("weight", "offset"),
    [("1.00", 0), ("0.70", 26), ("0.90", 7), ("0.80", 15)],
)
def test_rank_offset_matches_the_lld_examples(weight: str, offset: int) -> None:
    """Weight 1.00 leaves a list unshifted; 0.70 costs 26 places at k=60."""
    assert rank_offset(Decimal(weight), 60) == offset


def test_rank_offset_rounds_half_up_not_to_even() -> None:
    """k=60, w=0.96 is exactly 2.5: half up gives 3, banker's rounding would give 2."""
    assert rank_offset(Decimal("0.96"), 60) == 3
    assert rank_offset(Decimal("0.98"), 100) == 2  # 2.04 -> 2
    assert rank_offset(Decimal("0.99"), 50) == 1  # 0.505 -> 1


def test_rank_offset_scales_with_k_and_rejects_bad_weights() -> None:
    """The prior moves with k (LD-24); a weight outside (0, 1] is refused."""
    assert rank_offset(Decimal("0.70"), 120) == 51
    for bad in ("0", "1.01", "-0.5"):
        with pytest.raises(ValueError):
            rank_offset(Decimal(bad), 60)


def test_rrf_scores_are_reciprocal_ranks_across_lists() -> None:
    """A chunk in both lists outranks a better-placed chunk in one; both halves are labelled."""
    a, b, c = chunk(1), chunk(2), chunk(3)
    fused = rrf_fuse([RankedList("dense", [a, b]), RankedList("lexical_original", [b, c])], 60)
    assert [x.chunk_id for x in fused] == [b.chunk_id, a.chunk_id, c.chunk_id]
    assert fused[0].source_half == "both" and fused[1].source_half == "dense"
    assert fused[0].fused_score == pytest.approx(1 / 62 + 1 / 61)
    assert fused[1].fused_score == pytest.approx(1 / 61)


def test_rrf_collection_weight_is_a_rank_prior_not_a_multiplier() -> None:
    """A top chunk of a 0.70 collection (offset 26) loses to a rank-20 full-weight chunk."""
    light = chunk(1, weight="0.70")
    full = [chunk(100 + i) for i in range(30)]
    fused = rrf_fuse([RankedList("dense", [light]), RankedList("dense_b", full)], 60)
    assert light.chunk_id not in [c.chunk_id for c in fused[:20]]
    assert fused[-1].chunk_id != light.chunk_id or len(fused) < 30
    assert [c.chunk_id for c in fused].index(light.chunk_id) in range(20, 30)


def test_rrf_dedups_identical_text_keeping_the_highest_ranked_copy() -> None:
    """LD-96: repeated chunk text across the candidate set collapses; the better copy wins."""
    first = chunk(1, text="Same wording.")
    other = chunk(2, text="Different.")
    copy = chunk(3, text="Same wording.")
    fused = rrf_fuse([RankedList("dense", [first, other, copy])], 60)
    assert [c.chunk_id for c in fused] == [first.chunk_id, other.chunk_id]


def test_rrf_rank_shift_raises_a_lists_prior_but_never_below_k() -> None:
    """``intent = reference`` boosts the lexical lists (§6.5); the denominator floors at k."""
    a, b = chunk(1), chunk(2)
    plain = rrf_fuse([RankedList("dense", [a]), RankedList("lexical_original", [b])], 60)
    boosted = rrf_fuse(
        [RankedList("dense", [a]), RankedList("lexical_original", [b], rank_shift=-10)], 60
    )
    assert [c.chunk_id for c in plain][0] == a.chunk_id
    assert boosted[0].chunk_id == b.chunk_id and boosted[0].fused_score == pytest.approx(1 / 60)


def test_rrf_empty_input_and_deterministic_ties() -> None:
    """No lists, no output; equal scores order by chunk id."""
    assert rrf_fuse([], 60) == []
    x, y = chunk(2), chunk(1)
    out = rrf_fuse([RankedList("dense", [x]), RankedList("dense2", [y])], 60)
    assert [c.chunk_id for c in out] == sorted(c.chunk_id for c in (x, y))


# --- budget (§6.8) ----------------------------------------------------------------------------


def budget(**kw: float | int):  # type: ignore[no-untyped-def]
    """A 100k-token window with the LLD fractions."""
    args: dict[str, float | int] = dict(
        output_fraction=0.25,
        user_input_max_fraction=0.10,
        skeleton_tokens=5_000,
        user_input_tokens=1_000,
    )
    args.update(kw)
    return plan_budget(100_000, **args)  # type: ignore[arg-type]


def test_output_is_reserved_first_and_evidence_is_the_residual() -> None:
    """RET-6: 25% output, then skeleton and user input; the rest is evidence."""
    b = budget()
    assert (b.output_reserve_tokens, b.evidence_tokens) == (25_000, 100_000 - 25_000 - 6_000)
    assert b.as_dict()["window_tokens"] == 100_000


def test_user_input_over_ten_percent_is_413_never_trimmed() -> None:
    """HLD §7.7: excess is rejected."""
    with pytest.raises(DomainError) as e:
        budget(user_input_tokens=10_001)
    assert e.value.code is ErrorCode.USER_INPUT_OVER_BUDGET
    assert budget(user_input_tokens=10_000).user_input_tokens == 10_000


def test_skeleton_over_fifteen_percent_is_pack_invalid() -> None:
    """LD-63: refused at pack resolution with 422."""
    with pytest.raises(DomainError) as e:
        budget(skeleton_tokens=15_001)
    assert e.value.code is ErrorCode.PACK_INVALID


def test_candidate_depth_is_bounded_by_the_residual() -> None:
    """``min(profile_depth, residual / mean_chunk_tokens x overfetch)``, at least one."""
    big = budget()
    assert candidate_depth(24, big, 350, 1.5) == 24
    tight = plan_budget(
        1_000,
        output_fraction=0.25,
        user_input_max_fraction=0.1,
        skeleton_tokens=0,
        user_input_tokens=10,
    )
    assert candidate_depth(24, tight, 350, 1.5) == 3
    assert (
        candidate_depth(
            24,
            plan_budget(
                100,
                output_fraction=0.99,
                user_input_max_fraction=0.5,
                skeleton_tokens=0,
                user_input_tokens=0,
            ),
            350,
            1.5,
        )
        == 1
    )


def test_trim_drops_from_the_bottom_until_it_fits() -> None:
    """§6.8: the reranked order's tail goes first."""
    chunks = [chunk(i) for i in range(1, 6)]
    r = trim_to_budget(chunks, [100] * 5, 250, diversity_bound=5)
    assert [c.chunk_id for c in r.kept] == [c.chunk_id for c in chunks[:2]]
    assert r.refused is None and len(r.dropped) == 3


def test_diversity_bound_drops_a_documents_lowest_ranked_chunks() -> None:
    """One long specification cannot crowd out every other source."""
    doc = uuid4()
    chunks = [chunk(1, doc=doc), chunk(2, doc=doc), chunk(3, doc=doc), chunk(4)]
    r = trim_to_budget(chunks, [10] * 4, 1_000, diversity_bound=2)
    assert [c.chunk_id for c in r.kept] == [
        chunks[0].chunk_id,
        chunks[1].chunk_id,
        chunks[3].chunk_id,
    ]


def test_a_trim_that_would_drop_required_evidence_refuses_instead() -> None:
    """§6.8: Insufficient, never a silent drop of the only chunk of a required role."""
    chunks = [chunk(1), chunk(2), chunk(3, role="drawing")]
    r = trim_to_budget(
        chunks,
        [100, 100, 100],
        200,
        diversity_bound=5,
        protection=Protection(required_roles=frozenset({"drawing"})),
    )
    assert r.refused == "trim_would_drop_required_evidence" and len(r.kept) == 3
    ok = trim_to_budget(
        chunks,
        [100, 100, 100],
        200,
        diversity_bound=5,
        protection=Protection(required_roles=frozenset({"specification"})),
    )
    assert ok.refused is None  # a specification chunk survives


def test_a_mandatory_clause_is_never_dropped() -> None:
    """A chunk carrying a mandatory clause protects the set from trimming it."""
    chunks = [chunk(1), chunk(2, clause="4.2")]
    r = trim_to_budget(
        chunks,
        [100, 100],
        100,
        diversity_bound=5,
        protection=Protection(mandatory_clauses=frozenset({"4.2"})),
    )
    assert r.refused is not None


# --- sufficiency and required input (§6.9) --------------------------------------------------

TH = SufficiencyThreshold(min_top_score=0.5, min_chunks=2, chunk_score_floor=0.2)


def test_judge_reranked_uses_top_score_and_count_above_floor() -> None:
    """D-15: score-based; every non-sufficient verdict carries a reason (RS-5)."""
    assert judge([0.9, 0.3], TH, reranked=True).verdict is RetrievalVerdict.SUFFICIENT
    low_top = judge([0.4, 0.3], TH, reranked=True)
    assert (low_top.verdict, low_top.reason) == (
        RetrievalVerdict.INSUFFICIENT,
        "top_score_below_threshold",
    )
    few = judge([0.9, 0.1], TH, reranked=True)
    assert few.reason == "too_few_chunks_above_floor"
    assert judge([], TH, reranked=True).reason == "no_evidence_retrieved"


def test_judge_without_rerank_judges_only_the_count() -> None:
    """Fused RRF scores are not comparable with a reranker threshold."""
    assert judge([None, None], TH, reranked=False).verdict is RetrievalVerdict.SUFFICIENT
    assert judge([None], TH, reranked=False).reason == "too_few_chunks"


def test_required_inputs_report_missing_roles_entities_and_empty_collections() -> None:
    """LD-100: zero count means missing/unresolved; ``blocking`` follows the rule."""
    rule = RequiredInputRule(
        ("drawing", "specification"), ("standard",), non_empty_collections=True
    )
    empty = uuid4()
    gap = evaluate_required_inputs(
        rule,
        role_counts={"specification": 4},
        entity_counts={"standard": 0},
        empty_collections=[empty],
    )
    assert gap.missing_roles == ("drawing",) and gap.unresolved_entities == ("standard",)
    assert gap.empty_collections == (empty,) and gap.blocking


def test_empty_collections_are_named_but_block_only_when_the_rule_says_so() -> None:
    """SCOPE-12: reported, never silent; blocking is the rule's call."""
    empty = uuid4()
    open_rule = evaluate_required_inputs(
        RequiredInputRule(), role_counts={}, entity_counts={}, empty_collections=[empty]
    )
    assert open_rule.empty_collections == (empty,) and not open_rule.blocking
    strict = evaluate_required_inputs(
        RequiredInputRule(non_empty_collections=True),
        role_counts={},
        entity_counts={},
        empty_collections=[empty],
    )
    assert strict.blocking
    clean = evaluate_required_inputs(
        RequiredInputRule(("drawing",)),
        role_counts={"drawing": 1},
        entity_counts={},
        empty_collections=[],
    )
    assert not clean.blocking and clean.missing_roles == ()


# --- text -------------------------------------------------------------------------------------


def test_excerpt_snaps_to_a_sentence_end_within_the_limit() -> None:
    """§7.7: at most 240 characters, ending at a sentence where one is reasonable."""
    text = ("First sentence is here. " * 5) + "A very long tail without end " * 20
    out = snap_excerpt(text, 240)
    assert len(out) <= 240 and out.endswith(".")
    assert snap_excerpt("Short text.", 240) == "Short text."


def test_excerpt_never_splits_a_word_and_normalises_whitespace() -> None:
    """No sentence end: cut at a word boundary."""
    out = snap_excerpt("word " * 100, 50)
    assert len(out) <= 50 and out.split(" ")[-1] == "word" and "  " not in out
    assert snap_excerpt("a\n\n  b", 240) == "a b"


def test_query_normalisation_for_cache_keys() -> None:
    """Case-folded, whitespace-collapsed."""
    assert normalise_query("  Crane   ERECTION\nAccess ") == "crane erection access"
