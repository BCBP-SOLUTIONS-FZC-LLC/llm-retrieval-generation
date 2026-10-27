"""Ladder decisions, score and tier derivation, completeness, combined verdict (§8)."""

from __future__ import annotations

from decimal import Decimal

import pytest

from rag.domain.shared.enums import ClaimOutcome, CompletenessVerdict, EvidenceTier
from rag.domain.verification.claims import parse_claims
from rag.domain.verification.completeness import check_completeness
from rag.domain.verification.scoring import (
    Disposition,
    TierThresholds,
    combined_verdict,
    derive_score_and_tier,
)
from rag.domain.verification.tiers import (
    LadderThresholds,
    Tier1,
    Tier2,
    overlap,
    settle_negative,
    tier0_disagreement,
    tier1,
    tier2,
    top_candidates,
    value_in_text,
)

TH = LadderThresholds()


def test_parse_claims_extracts_refs_gaps_and_ignores_fragments() -> None:
    text = (
        "The access route is via gate 4 [c-01]. Night work is barred after 22:00 [c-02, c-03]. "
        "Heading\n[GAP: no retention period in the evidence] Retention is unknown."
    )
    claims = parse_claims(text)
    assert [c.ordinal for c in claims] == [1, 2, 3]
    assert claims[0].cited == ("c-01",) and claims[1].cited == ("c-02", "c-03")
    assert claims[2].gap and "[GAP" not in claims[2].text


def test_tier1_supports_at_the_overlap_threshold_and_routes_below() -> None:
    chunk = {"a": "crane erection requires a lifting plan approved by the engineer"}
    hit = tier1("crane erection requires approved lifting plan", ["a"], chunk, TH)
    assert hit.decision is Tier1.SUPPORTED and hit.best_cited >= 0.6
    miss = tier1("night work restrictions apply on weekends", ["a"], chunk, TH)
    assert miss.decision is Tier1.ROUTE_TO_TIER2 and miss.correction is None


def test_tier1_remembers_the_chunk_that_does_support_a_miscited_claim() -> None:
    chunks = {"a": "unrelated paving specification", "b": "gate 4 is the access route for cranes"}
    r = tier1("gate 4 is the access route for cranes", ["a"], chunks, TH)
    assert r.decision is Tier1.ROUTE_TO_TIER2 and r.correction == "b"
    assert settle_negative(r.correction) == (ClaimOutcome.MISCITED, ("b",))
    assert settle_negative(None) == (ClaimOutcome.UNSUPPORTED, ())


@pytest.mark.parametrize(
    ("score", "expected"),
    [
        (0.95, Tier2.SUPPORTED),
        (0.70, Tier2.SUPPORTED),  # above 0.70 is supported (§8.1); the boundary belongs to it
        (0.699, Tier2.UNCERTAIN),
        (0.55, Tier2.UNCERTAIN),
        (0.40, Tier2.UNCERTAIN),  # 0.40 to 0.70 routes to tier 3
        (0.399, Tier2.UNSUPPORTED),
        (0.0, Tier2.UNSUPPORTED),
    ],
)
def test_tier2_routing_boundaries(score: float, expected: Tier2) -> None:
    assert tier2(score, TH) is expected


def test_tier0_disagreement_is_claim_level_both_ways() -> None:
    a = parse_claims("The bridge is 40 metres long [c-01]. Piling starts in March [c-02].")
    same = parse_claims("Piling starts in March [c-02]. The bridge is 40 metres long [c-01].")
    other = parse_claims("The bridge is 90 metres wide [c-01]. Tunnels open at dawn daily [c-02].")
    assert tier0_disagreement(a, same, TH) == 0.0
    assert tier0_disagreement(a, other, TH) > TH.tier0_disagreement
    assert tier0_disagreement([], [], TH) == 0.0


def test_top_candidates_excludes_cited_and_zero_overlap() -> None:
    chunks = {"a": "alpha beta", "b": "alpha gamma delta", "c": "zzz"}
    assert top_candidates("alpha gamma", ["a"], chunks) == ["b"]
    assert overlap("", "x") == 0.0


def test_value_in_text_is_separator_tolerant() -> None:
    assert value_in_text(4820000, "Contract value: EUR 4,820,000 excl. VAT")
    assert value_in_text("Rotterdam", "Site: rotterdam harbour")
    assert not value_in_text(90, "Duration 30 days")
    assert not value_in_text("", "anything")


# --- score and tier (§8.4, VER-4, VER-5, GR-4) -------------------------------------------------


def score(**kw: int | bool) -> tuple[Decimal | None, EvidenceTier]:
    base: dict[str, int | bool] = dict(
        supported=0, unsupported=0, miscited=0, gaps=0, compliance=False
    )
    r = derive_score_and_tier(**{**base, **kw})  # type: ignore[arg-type]
    return r.score, r.tier


def test_pure_gaps_are_null_and_not_computed_never_zero() -> None:
    assert score(gaps=3) == (None, EvidenceTier.NOT_COMPUTED)  # VER-5
    assert score() == (None, EvidenceTier.NOT_COMPUTED)
    assert score(unsupported=2) == (Decimal("0.000"), EvidenceTier.INSUFFICIENT)  # zero is not null


def test_a_verification_that_did_not_run_is_not_computed() -> None:
    r = derive_score_and_tier(
        supported=5, unsupported=0, miscited=0, gaps=0, compliance=False, verification_ran=False
    )
    assert (r.score, r.tier) == (None, EvidenceTier.NOT_COMPUTED)


def test_gaps_leave_the_denominator_and_miscited_count_as_supported() -> None:
    # 10 supported + 1 miscited, 1 gap: (10+1)/11 = 1.0
    assert score(supported=10, miscited=1, gaps=1) == (Decimal("1.000"), EvidenceTier.SUPPORTED)


def test_exact_boundaries_070_and_095() -> None:
    assert score(supported=7, unsupported=3)[1] is EvidenceTier.PARTIAL  # exactly 0.70
    assert score(supported=69, unsupported=31)[1] is EvidenceTier.INSUFFICIENT  # 0.69
    assert score(supported=19, miscited=0, unsupported=1)[1] is EvidenceTier.PARTIAL  # 0.95, 1 bad
    assert score(supported=95, miscited=0, unsupported=5)[1] is EvidenceTier.PARTIAL  # 0.95 + bad
    assert score(supported=20)[1] is EvidenceTier.SUPPORTED
    assert score(supported=94, unsupported=6)[1] is EvidenceTier.PARTIAL  # 0.94


def test_non_compliance_at_095_with_one_unsupported_is_partial_not_supported() -> None:
    # one unsupported claim of 21: score .952 clears 0.95 but SUPPORTED demands none unsupported
    s, tier = score(supported=19, miscited=1, unsupported=1)
    assert s == Decimal("0.952") and tier is EvidenceTier.PARTIAL


def test_compliance_gate_any_unsupported_claim_is_insufficient() -> None:
    """GR-4: the interim gate is absolute, whatever the score."""
    s, tier = score(supported=99, unsupported=1, compliance=True)
    assert s == Decimal("0.990") and tier is EvidenceTier.INSUFFICIENT
    assert score(supported=99, compliance=True)[1] is EvidenceTier.SUPPORTED


def test_thresholds_are_configuration() -> None:
    strict = TierThresholds(partial=0.9, supported=0.99)
    r = derive_score_and_tier(
        supported=19, unsupported=1, miscited=0, gaps=0, compliance=False, thresholds=strict
    )
    assert r.tier is EvidenceTier.PARTIAL  # 0.95 is below a 0.99 gate, above the 0.9 floor


def test_the_tier_recomputes_from_the_stored_counts() -> None:
    """VER-4: arithmetic over the row, so the roll-up is reproducible."""
    first = score(supported=13, unsupported=2, miscited=1, gaps=2)
    again = score(supported=13, unsupported=2, miscited=1, gaps=2)
    assert first == again


# --- completeness (§8.2, CC-1) -----------------------------------------------------------------


def test_completeness_verdicts_and_the_70_percent_line() -> None:
    items = ["retention period", "liability cap", "governing law", "payment terms", "warranty"]
    text = "Retention period is 5 years. The liability cap is EUR 1m. Governing law is Dutch."
    r = check_completeness(text, items)
    assert (r.items_total, r.items_addressed) == (5, 3)
    assert r.verdict is CompletenessVerdict.INSUFFICIENT  # 60% < 70%
    assert r.missed_items == ("payment terms", "warranty")
    four = check_completeness(text + " Payment terms: net 30.", items)
    assert four.verdict is CompletenessVerdict.PARTIAL and four.missed_items == ("warranty",)
    assert check_completeness(text + " Payment terms net 30; warranty 2 years.", items).verdict is (
        CompletenessVerdict.COMPLETE
    )


def test_an_empty_checklist_is_not_computed_never_complete() -> None:
    r = check_completeness("anything", [])
    assert r.verdict is CompletenessVerdict.NOT_COMPUTED and r.items_total == 0


def test_token_level_matching_addresses_reordered_phrases() -> None:
    assert check_completeness(
        "The period of retention is five years.", ["retention period"]
    ).verdict is (CompletenessVerdict.COMPLETE)


# --- combined verdict (§8.3) -------------------------------------------------------------------


@pytest.mark.parametrize(
    ("tier", "comp", "expected"),
    [
        (EvidenceTier.SUPPORTED, CompletenessVerdict.COMPLETE, Disposition.SURFACE),
        (EvidenceTier.SUPPORTED, CompletenessVerdict.NOT_COMPUTED, Disposition.SURFACE),
        (EvidenceTier.SUPPORTED, CompletenessVerdict.PARTIAL, Disposition.SURFACE_FLAGGED),
        (EvidenceTier.PARTIAL, CompletenessVerdict.COMPLETE, Disposition.SURFACE_FLAGGED),
        (EvidenceTier.NOT_COMPUTED, CompletenessVerdict.COMPLETE, Disposition.SURFACE_FLAGGED),
        (EvidenceTier.INSUFFICIENT, CompletenessVerdict.COMPLETE, Disposition.RETRY),
        (EvidenceTier.SUPPORTED, CompletenessVerdict.INSUFFICIENT, Disposition.RETRY),
    ],
)
def test_combined_verdict(
    tier: EvidenceTier, comp: CompletenessVerdict, expected: Disposition
) -> None:
    """NOT_COMPUTED is never a pass (AOB-2): it surfaces flagged."""
    assert combined_verdict(tier, comp) is expected
