"""``VerifySection``: the tiered ladder end to end (LLD §8), reusable by workflows.

It receives the retrieval set as data (``EvidenceChunk``s with their full text) and never reads
retrieval itself (DEP-4). Model calls reach it through the ports on the request, so the caller
fixes the Gateway context and the Model Profiles.

Numbering follows §8.1: tier 0 self-consistency, 1 citation-span overlap, 2 entailment,
3 judge. ``tiers_run`` and ``tier_decided`` persist these numbers.
"""

from __future__ import annotations

import asyncio
import random
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal

from rag.application.shared.observability import GenerationObserver, NullGenerationObserver
from rag.domain.shared.enums import ClaimOutcome, CompletenessVerdict, EvidenceTier
from rag.domain.shared.errors import DomainError, ErrorCode
from rag.domain.verification.claims import Claim, parse_claims
from rag.domain.verification.completeness import Completeness, check_completeness
from rag.domain.verification.ports import (
    Entailer,
    Judge,
    Resampler,
    VerificationUnavailable,
)
from rag.domain.verification.scoring import (
    Disposition,
    TierThresholds,
    combined_verdict,
    derive_score_and_tier,
)
from rag.domain.verification.tiers import (
    EvidenceChunk,
    LadderThresholds,
    Tier1,
    Tier2,
    settle_negative,
    tier0_disagreement,
    tier1,
    tier2,
    top_candidates,
)

MAX_ENTAIL_PASSAGES = 3
"""Cited chunks scored per claim at tier 2: bounds the call count of a heavily cited claim."""


@dataclass(frozen=True, slots=True)
class VerificationConfig:
    """Ladder tunables: LLD §8 and §17 defaults, none of them calibrated (CFG-1)."""

    ladder: LadderThresholds = field(default_factory=LadderThresholds)
    tiers: TierThresholds = field(default_factory=TierThresholds)
    completeness_partial: float = 0.70
    tier0_sample_rate: float = 0.10
    concurrency: int = 8


@dataclass(frozen=True, slots=True)
class VerificationRequest:
    """One section's text, the evidence it may be scored against, and the model ports.

    ``refs`` maps the provisional ``c-NN`` markers to chunk ids. ``entailer``, ``judge`` and
    ``resampler`` are ``None`` where no Gateway is wired: a compliance section then fails
    (GR-3), any other is ``NOT_COMPUTED``. ``section_label`` is a pack-declared section type or
    ``None`` (a metric label, never client input).
    """

    text: str
    evidence: Sequence[EvidenceChunk]
    refs: Mapping[str, str]
    mandatory_clauses: Iterable[str] = ()
    compliance: bool = False
    entailer: Entailer | None = None
    judge: Judge | None = None
    resampler: Resampler | None = None
    section_label: str | None = None


@dataclass(frozen=True, slots=True)
class ClaimResult:
    """One verified claim (``claim_verifications`` row, ``Citation`` frame entry)."""

    ordinal: int
    outcome: ClaimOutcome
    cited_chunk_ids: tuple[str, ...]
    corrected_chunk_ids: tuple[str, ...]
    tier_decided: int


@dataclass(frozen=True, slots=True)
class VerificationResult:
    """The groundedness report's content, the completeness check and the combined verdict."""

    claims: tuple[ClaimResult, ...]
    supported: int
    unsupported: int
    miscited: int
    gaps: int
    tiers_run: tuple[int, ...]
    score: Decimal | None
    tier: EvidenceTier
    completeness: Completeness
    disposition: Disposition
    judge_model_profile_id: str | None = None
    markers: tuple[dict[str, str], ...] = ()
    tier0_disagreement: float | None = None

    @property
    def claims_total(self) -> int:
        """``supported + unsupported + miscited + gaps`` (``ck_gr_totals``)."""
        return self.supported + self.unsupported + self.miscited + self.gaps


def _counts(claims: Sequence[ClaimResult]) -> tuple[int, int, int, int]:
    n = {o: sum(1 for c in claims if c.outcome is o) for o in ClaimOutcome}
    return (
        n[ClaimOutcome.SUPPORTED],
        n[ClaimOutcome.UNSUPPORTED],
        n[ClaimOutcome.MISCITED],
        n[ClaimOutcome.GAP],
    )


class VerifySection:
    """Verifies one generated section and returns the evidence tier it earns.

    ``VerifySection(config, observer).execute(request)``. Raises ``503 INFERENCE_UNAVAILABLE`` for
    a compliance section whose mandated tier cannot run (GR-3, GEN-7); for any other section the
    same failure yields a result with tier ``NOT_COMPUTED`` and a marker (§14.4).
    """

    def __init__(
        self,
        config: VerificationConfig | None = None,
        observer: GenerationObserver | None = None,
        rand: Callable[[], float] = random.random,
    ) -> None:
        self._cfg = config or VerificationConfig()
        self._obs = observer or NullGenerationObserver()
        self._rand = rand

    async def execute(self, req: VerificationRequest) -> VerificationResult:
        """Run the ladder, completeness and the combined verdict."""
        completeness = check_completeness(
            req.text, req.mandatory_clauses, partial_threshold=self._cfg.completeness_partial
        )
        for item in completeness.missed_items:
            self._obs.completeness_missed(req.section_label, item)
        try:
            claims, tiers, judge_model, ratio = await self._ladder(req)
        except VerificationUnavailable as exc:
            if req.compliance:  # a mandated tier is never dropped (GR-3, GEN-7)
                raise DomainError(
                    ErrorCode.INFERENCE_UNAVAILABLE, "Mandated verification could not run."
                ) from exc
            return self._finish(
                req, (), (), None, completeness, ran=False, markers=(_NOT_RUN_MARKER,)
            )
        return self._finish(req, claims, tiers, judge_model, completeness, ran=True, ratio=ratio)

    def _finish(
        self,
        req: VerificationRequest,
        claims: Sequence[ClaimResult],
        tiers: Iterable[int],
        judge_model: str | None,
        completeness: Completeness,
        *,
        ran: bool,
        ratio: float | None = None,
        markers: tuple[dict[str, str], ...] = (),
    ) -> VerificationResult:
        s, u, m, g = _counts(claims)
        scored = derive_score_and_tier(
            supported=s,
            unsupported=u,
            miscited=m,
            gaps=g,
            compliance=req.compliance,
            thresholds=self._cfg.tiers,
            verification_ran=ran,
        )
        for c in claims:
            self._obs.claim_outcome(c.outcome.value)
            self._obs.tier_decided(c.tier_decided)
            if c.outcome is ClaimOutcome.GAP:
                self._obs.gap_marker()
        self._obs.groundedness(None if scored.score is None else float(scored.score))
        self._obs.evidence_tier(scored.tier.value)
        return VerificationResult(
            claims=tuple(claims),
            supported=s,
            unsupported=u,
            miscited=m,
            gaps=g,
            tiers_run=tuple(sorted(set(tiers))),
            score=scored.score,
            tier=scored.tier,
            completeness=completeness,
            disposition=combined_verdict(scored.tier, completeness.verdict),
            judge_model_profile_id=judge_model,
            markers=markers,
            tier0_disagreement=ratio,
        )

    async def _ladder(
        self, req: VerificationRequest
    ) -> tuple[list[ClaimResult], set[int], str | None, float | None]:
        th = self._cfg.ladder
        chunks = {c.chunk_id: c.text for c in req.evidence}
        claims = parse_claims(req.text)
        tiers: set[int] = {1} if claims else set()
        force, ratio = 1, None
        if req.compliance or self._rand() < self._cfg.tier0_sample_rate:
            if req.resampler is None:
                raise VerificationUnavailable("tier 0 has no second generation to diff")
            second = parse_claims(await req.resampler.sample())
            ratio = tier0_disagreement(claims, second, th)
            tiers.add(0)
            self._obs.tier0_ratio(ratio)
            if ratio > th.tier0_disagreement:  # routes up: tier 3 on compliance, tier 2 otherwise
                force = 3 if req.compliance else 2
        sem = asyncio.Semaphore(self._cfg.concurrency)
        models: list[str] = []

        async def one(claim: Claim) -> tuple[ClaimResult, set[int]]:
            async with sem:
                return await self._claim(claim, req, chunks, force, models)

        done = await asyncio.gather(*(one(c) for c in claims))
        for _, used in done:
            tiers |= used
        return [r for r, _ in done], tiers, (models[-1] if models else None), ratio

    async def _claim(
        self,
        claim: Claim,
        req: VerificationRequest,
        chunks: Mapping[str, str],
        force: int,
        models: list[str],
    ) -> tuple[ClaimResult, set[int]]:
        th = self._cfg.ladder
        cited = tuple(dict.fromkeys(req.refs[r] for r in claim.cited if r in req.refs))

        def result(outcome: ClaimOutcome, tier: int, fixed: tuple[str, ...] = ()) -> ClaimResult:
            return ClaimResult(claim.ordinal, outcome, cited, fixed, tier)

        if claim.gap:  # the model declined to assert: counted, not a failure (§8.1)
            return result(ClaimOutcome.GAP, 1), {1}
        t1 = tier1(claim.text, cited, chunks, th)
        if t1.decision is Tier1.SUPPORTED and force < 2:
            return result(ClaimOutcome.SUPPORTED, 1), {1}
        used = {1, 2}
        if req.entailer is None:
            raise VerificationUnavailable("no entailment scorer is wired")
        score = 0.0
        scored = [
            req.entailer.score(claim.text, EvidenceChunk(c, chunks[c]))
            for c in cited[:MAX_ENTAIL_PASSAGES]
            if c in chunks
        ]
        for entailment in await asyncio.gather(*scored):
            score = max(score, entailment.score)
            if entailment.model:
                models.append(entailment.model)
        verdict2 = tier2(score, th)
        confirm = req.compliance and verdict2 is not Tier2.SUPPORTED  # an unsupported claim
        if force < 3 and verdict2 is not Tier2.UNCERTAIN and not confirm:  # blocks the section
            if verdict2 is Tier2.SUPPORTED:
                return result(ClaimOutcome.SUPPORTED, 2), used
            outcome, fixed = settle_negative(t1.correction)
            return result(outcome, 2, fixed), used
        if req.judge is None:
            raise VerificationUnavailable("no judge is wired")
        used.add(3)
        candidates = top_candidates(claim.text, cited, chunks)
        if t1.correction and t1.correction not in candidates:
            candidates.append(t1.correction)
        verdict3 = await req.judge.judge(
            claim.text,
            [EvidenceChunk(c, chunks[c]) for c in cited if c in chunks],
            [EvidenceChunk(c, chunks[c]) for c in candidates],
        )
        if verdict3.model:
            models.append(verdict3.model)
        if verdict3.supported:
            return result(ClaimOutcome.SUPPORTED, 3), used
        valid = tuple(c for c in verdict3.corrected_chunk_ids if c in candidates)
        outcome, fixed = settle_negative(valid[0] if valid else t1.correction)
        return result(outcome, 3, fixed), used


_NOT_RUN_MARKER = {
    "stage": "verification",
    "reason": "judge_unavailable",
    "applied_fallback": "not_computed",
}

__all__ = [
    "ClaimResult",
    "CompletenessVerdict",
    "VerificationConfig",
    "VerificationRequest",
    "VerificationResult",
    "VerifySection",
]
