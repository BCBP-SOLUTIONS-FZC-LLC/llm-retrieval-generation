"""``VerifySection``: the ladder routes as §8.1 says, and a mandated tier is never dropped."""

from __future__ import annotations

from collections.abc import Sequence

import pytest

from rag.application.verification.verify import (
    VerificationConfig,
    VerificationRequest,
    VerifySection,
)
from rag.domain.shared.enums import ClaimOutcome, CompletenessVerdict, EvidenceTier
from rag.domain.shared.errors import DomainError, ErrorCode
from rag.domain.verification.ports import Entailment, JudgeVerdict, VerificationUnavailable
from rag.domain.verification.scoring import Disposition
from rag.domain.verification.tiers import EvidenceChunk

CHUNKS = [
    EvidenceChunk("k1", "crane erection requires an approved lifting plan"),
    EvidenceChunk("k2", "night work is barred between 22:00 and 06:00"),
    EvidenceChunk("k3", "paving uses asphalt on the car park"),
]
REFS = {"c-01": "k1", "c-02": "k2", "c-03": "k3"}


class Entail:
    def __init__(self, score: float = 0.9, error: bool = False) -> None:
        self.score_value, self.error, self.calls = score, error, 0

    async def score(self, claim: str, passage: EvidenceChunk) -> Entailment:
        self.calls += 1
        if self.error:
            raise VerificationUnavailable("down")
        return Entailment(self.score_value, "judge-small")


class Judge:
    def __init__(self, supported: bool = True, corrected: tuple[str, ...] = ()) -> None:
        self.verdict = JudgeVerdict(supported, corrected, "judge-large")
        self.calls: list[tuple[str, list[str], list[str]]] = []

    async def judge(
        self, claim: str, cited: Sequence[EvidenceChunk], candidates: Sequence[EvidenceChunk]
    ) -> JudgeVerdict:
        self.calls.append((claim, [c.chunk_id for c in cited], [c.chunk_id for c in candidates]))
        return self.verdict


class Resample:
    def __init__(self, text: str = "", error: bool = False) -> None:
        self.text, self.error, self.calls = text, error, 0

    async def sample(self) -> str:
        self.calls += 1
        if self.error:
            raise VerificationUnavailable("down")
        return self.text


def req(text: str, **kw: object) -> VerificationRequest:
    base: dict[str, object] = dict(evidence=CHUNKS, refs=REFS)
    return VerificationRequest(text=text, **{**base, **kw})  # type: ignore[arg-type]


def verifier(sample: float = 0.0) -> VerifySection:
    return VerifySection(VerificationConfig(tier0_sample_rate=sample), rand=lambda: 0.5)


SUPPORTED = "Crane erection requires an approved lifting plan [c-01]."


async def test_tier_1_settles_a_well_cited_claim_with_no_model_call() -> None:
    e, j = Entail(), Judge()
    r = await verifier().execute(req(SUPPORTED, entailer=e, judge=j))
    assert r.tiers_run == (1,) and e.calls == 0 and j.calls == []
    assert [(c.outcome, c.tier_decided) for c in r.claims] == [(ClaimOutcome.SUPPORTED, 1)]
    assert r.tier is EvidenceTier.SUPPORTED and r.score is not None


async def test_a_low_overlap_claim_routes_to_tier_2_and_a_high_score_supports_it() -> None:
    e = Entail(0.85)
    r = await verifier().execute(req("Lifting needs sign-off by an engineer [c-01].", entailer=e))
    assert r.tiers_run == (1, 2) and e.calls == 1
    assert r.claims[0].outcome is ClaimOutcome.SUPPORTED and r.claims[0].tier_decided == 2
    assert r.judge_model_profile_id == "judge-small"


async def test_an_entailment_score_between_040_and_070_routes_to_tier_3() -> None:
    e, j = Entail(0.55), Judge(supported=True)
    r = await verifier().execute(
        req("Lifting needs sign-off by an engineer [c-01].", entailer=e, judge=j)
    )
    assert r.tiers_run == (1, 2, 3) and len(j.calls) == 1
    assert (r.claims[0].outcome, r.claims[0].tier_decided) == (ClaimOutcome.SUPPORTED, 3)
    assert r.judge_model_profile_id == "judge-large"


async def test_low_entailment_with_a_better_chunk_elsewhere_is_miscited_and_corrected() -> None:
    r = await verifier().execute(
        req(
            "Night work is barred between 22:00 and 06:00 [c-03].",
            entailer=Entail(0.05),
            judge=Judge(),
        )
    )
    (c,) = r.claims
    assert c.outcome is ClaimOutcome.MISCITED and c.corrected_chunk_ids == ("k2",)
    assert c.cited_chunk_ids == ("k3",) and c.tier_decided == 2
    assert (r.supported, r.miscited) == (0, 1)
    assert r.tier is EvidenceTier.SUPPORTED  # a corrected miscitation counts as supported (§8.4)


async def test_low_entailment_and_no_better_chunk_is_unsupported_and_retained() -> None:
    r = await verifier().execute(
        req("Submarines dive deeply at sea [c-01].", entailer=Entail(0.05))
    )
    assert r.claims[0].outcome is ClaimOutcome.UNSUPPORTED and r.unsupported == 1  # VER-2
    assert r.tier is EvidenceTier.INSUFFICIENT  # score 0 < 0.70


async def test_an_uncited_claim_is_supported_only_by_a_chunk_that_matches_it() -> None:
    r = await verifier().execute(
        req("Crane erection requires an approved lifting plan.", entailer=Entail(0.0))
    )
    assert r.claims[0].outcome is ClaimOutcome.MISCITED and r.claims[0].corrected_chunk_ids == (
        "k1",
    )


async def test_gap_claims_are_counted_and_leave_the_denominator() -> None:
    text = SUPPORTED + " [GAP: no retention period in the evidence]"
    r = await verifier().execute(req(text))
    assert (r.supported, r.gaps, r.claims_total) == (1, 1, 2)
    assert r.tier is EvidenceTier.SUPPORTED


async def test_compliance_runs_tier_0_and_a_confirming_judge_on_every_unsupported_claim() -> None:
    """GEN-7 / VER-1: tier 0 always; GR-4: an unsupported claim blocks, so tier 3 confirms it."""
    text = SUPPORTED + " Submarines dive deeply at sea [c-01]."
    rs, j = Resample(text), Judge(supported=False)
    r = await verifier().execute(
        req(text, compliance=True, entailer=Entail(0.05), judge=j, resampler=rs)
    )
    assert rs.calls == 1 and 0 in r.tiers_run and 3 in r.tiers_run
    assert r.tier is EvidenceTier.INSUFFICIENT and r.disposition is Disposition.RETRY
    assert r.tier0_disagreement == 0.0
    assert [c.outcome for c in r.claims] == [ClaimOutcome.SUPPORTED, ClaimOutcome.UNSUPPORTED]


async def test_tier_0_disagreement_over_20_percent_routes_compliance_to_tier_3() -> None:
    rs = Resample("Tunnels open at dawn daily. Bridges close at dusk nightly.")
    j = Judge(supported=True)
    r = await verifier().execute(
        req(SUPPORTED, compliance=True, entailer=Entail(0.9), judge=j, resampler=rs)
    )
    assert r.tier0_disagreement is not None and r.tier0_disagreement > 0.2
    assert len(j.calls) == 1 and r.claims[0].tier_decided == 3  # forced up even though tier 1 held


async def test_tier_0_disagreement_routes_a_non_compliance_section_to_tier_2() -> None:
    rs, e = Resample("Tunnels open at dawn daily. Bridges close at dusk nightly."), Entail(0.9)
    r = await verifier(sample=1.0).execute(req(SUPPORTED, entailer=e, judge=Judge(), resampler=rs))
    assert e.calls == 1 and r.claims[0].tier_decided == 2 and 3 not in r.tiers_run


async def test_tier_0_agreement_leaves_the_cheap_path() -> None:
    rs, e = Resample(SUPPORTED), Entail()
    r = await verifier(sample=1.0).execute(req(SUPPORTED, entailer=e, resampler=rs))
    assert e.calls == 0 and r.tiers_run == (0, 1)


async def test_a_compliance_section_fails_when_tier_0_cannot_run() -> None:
    for resampler in (None, Resample(error=True)):
        with pytest.raises(DomainError) as exc:
            await verifier().execute(
                req(
                    SUPPORTED,
                    compliance=True,
                    entailer=Entail(),
                    judge=Judge(),
                    resampler=resampler,
                )
            )
        assert exc.value.code is ErrorCode.INFERENCE_UNAVAILABLE


async def test_a_compliance_section_fails_when_the_judge_is_unreachable() -> None:
    with pytest.raises(DomainError) as exc:
        await verifier().execute(
            req("Lifting needs sign-off by an engineer [c-01].", compliance=True,
                entailer=Entail(error=True), judge=Judge(), resampler=Resample(SUPPORTED))
        )  # fmt: skip
    assert exc.value.code is ErrorCode.INFERENCE_UNAVAILABLE


async def test_another_section_with_an_unreachable_judge_is_not_computed_never_zero() -> None:
    r = await verifier().execute(
        req("Lifting needs sign-off by an engineer [c-01].", entailer=Entail(error=True))
    )
    assert r.tier is EvidenceTier.NOT_COMPUTED and r.score is None and r.claims == ()
    assert r.claims_total == 0 and r.markers[0]["reason"] == "judge_unavailable"
    assert r.disposition is Disposition.SURFACE_FLAGGED  # never a pass (AOB-2)


async def test_completeness_rides_along_and_a_missed_item_is_reported() -> None:
    r = await verifier().execute(
        req(SUPPORTED, mandatory_clauses=["lifting plan", "night work", "traffic management"])
    )
    assert r.completeness.verdict is CompletenessVerdict.INSUFFICIENT
    assert r.completeness.missed_items == ("night work", "traffic management")
    assert r.disposition is Disposition.RETRY  # grounded but Insufficient: the retry path (§8.3)


async def test_no_claims_at_all_is_not_computed() -> None:
    r = await verifier().execute(req("Heading"))
    assert r.tier is EvidenceTier.NOT_COMPUTED and r.tiers_run == ()
