"""§16.1 and §16.2 generation, verification and streaming metrics, Tier 3, OBS-1 and OBS-3.

Built only when metrics are enabled (``ENABLE_METRICS=true``, ``OBSERVABILITY_DOMAIN=llm``,
OQ-103).
Labels are bounded vocabularies: claim outcomes, ladder tiers, delivery states, bands,
pack-declared section types and items, Gateway model profiles. Never a tenant, user or id.

Deviations from the §16.2 table, forced by OBS-3: ``tier0_disagreement_ratio`` is a counter per
band (``tier0_disagreement_band_total``) because a ratio is not a duration, and
``verdict_frame_delivered_ratio`` is a running gauge.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from typing import Any

from fastapicommon.logging import get_logger
from fastapicommon.metrics import (
    MetricsNotInitializedError,
    new_counter,
    new_gauge,
    new_histogram,
)
from fastapicommon.telemetry import new_tracer, record_span_error

from rag.application.shared.observability import GenerationObserver, NullGenerationObserver
from rag.infrastructure.config import Settings

P = "llm_retrieval_generation_"
log = get_logger("rag.generation")

METRIC_NAMES = {
    "counter": [
        "claim_outcome_total",
        "verification_tier_total",
        "groundedness_band_total",
        "evidence_tier_total",
        "tier0_disagreement_band_total",
        "failure_policy_total",
        "completeness_item_missed_total",
        "gap_marker_total",
        "tokens_total",
        "delivery_state_total",
        "idempotent_replay_total",
    ],
    "histogram": [
        "answer_first_token_seconds",
        "answer_verified_seconds",
        "draft_verified_seconds",
        "extraction_duration_seconds",
        "first_frame_latency_seconds",
    ],
    "gauge": ["verdict_frame_delivered_ratio"],
}
"""Every collector name (without the prefix), so a test can validate them without a registry."""

LABELS = {
    "claim_outcome_total": ["outcome"],
    "verification_tier_total": ["tier_decided"],
    "groundedness_band_total": ["band"],
    "evidence_tier_total": ["tier"],
    "tier0_disagreement_band_total": ["band"],
    "failure_policy_total": ["action"],
    "completeness_item_missed_total": ["section_type", "item"],
    "tokens_total": ["task_class", "model_profile", "capability"],
    "delivery_state_total": ["delivery"],
    "first_frame_latency_seconds": ["suppressed"],
}
"""Label names per collector (the rest carry none)."""

_SCORE_BANDS = ((0.50, "<0.50"), (0.70, "0.50-0.70"), (0.95, "0.70-0.95"))
_RATIO_BANDS = ((0.0, "0"), (0.10, "0-0.10"), (0.20, "0.10-0.20"), (0.50, "0.20-0.50"))
_KINDS = {"answer": "answer_verified_seconds", "draft": "draft_verified_seconds"}


def score_band(score: float | None) -> str:
    """The counter-per-band form of a groundedness score (OBS-3); ``None`` is not computed."""
    if score is None:
        return "not_computed"
    return next((label for top, label in _SCORE_BANDS if score < top), ">=0.95")


def ratio_band(ratio: float) -> str:
    """The counter-per-band form of tier 0's disagreement ratio."""
    return next((label for top, label in _RATIO_BANDS if ratio <= top), ">0.50")


class PrometheusGenerationObserver:
    """``GenerationObserver`` over ``fastapicommon`` Tier-3 collectors."""

    def __init__(self) -> None:
        def counter(name: str, doc: str) -> Any:
            return new_counter(P + name, doc, LABELS.get(name, []))

        self._claims = counter("claim_outcome_total", "Claim outcomes")
        self._tiers = counter("verification_tier_total", "Ladder tier that settled a claim")
        self._bands = counter("groundedness_band_total", "Groundedness score bands")
        self._evidence = counter("evidence_tier_total", "Derived evidence tiers")
        self._tier0 = counter("tier0_disagreement_band_total", "Tier 0 disagreement bands")
        self._policy = counter("failure_policy_total", "Failure policy applied")
        self._missed = counter("completeness_item_missed_total", "Mandatory items missed")
        self._gaps = counter("gap_marker_total", "Gap claims")
        self._tokens = counter("tokens_total", "Tokens reported by the Gateway")
        self._delivery = counter("delivery_state_total", "Delivery states")
        self._replays = counter("idempotent_replay_total", "Idempotent replays")
        self._first_token = new_histogram(
            P + "answer_first_token_seconds", "Time to the first draft frame"
        )
        self._first_frame = new_histogram(
            P + "first_frame_latency_seconds", "Time to the first frame", ["suppressed"]
        )
        self._hist = {
            "answer": new_histogram(P + "answer_verified_seconds", "Answer to verdict frame"),
            "draft": new_histogram(P + "draft_verified_seconds", "Draft to verdict frame"),
            "extraction": new_histogram(P + "extraction_duration_seconds", "Extraction duration"),
        }
        self._delivered = new_gauge(P + "verdict_frame_delivered_ratio", "Verdict frames delivered")
        self._sent = self._ok = 0

    @asynccontextmanager
    async def span(self, name: str) -> AsyncIterator[None]:
        """Spans are the tracing wrapper's job."""
        yield

    def first_frame(self, seconds: float, suppressed: bool) -> None:
        """First frame latency; the first-token SLO excludes the blocking path (§16.1)."""
        self._first_frame.labels(suppressed=str(suppressed).lower()).observe(seconds)
        if not suppressed:
            self._first_token.observe(seconds)

    def verified(self, kind: str, seconds: float) -> None:
        """Request start to verdict."""
        if kind in self._hist:
            self._hist[kind].observe(seconds)

    def verdict_frame(self, delivered: bool) -> None:
        """Running ratio of verdict frames the client received (GEN-1)."""
        self._sent += 1
        self._ok += int(delivered)
        self._delivered.set(self._ok / self._sent)

    def delivery(self, state: str) -> None:
        """Delivery state of a written version."""
        self._delivery.labels(delivery=state).inc()

    def idempotent_replay(self) -> None:
        """A replay."""
        self._replays.inc()

    def claim_outcome(self, outcome: str) -> None:
        """One claim's outcome."""
        self._claims.labels(outcome=outcome).inc()

    def tier_decided(self, tier: int) -> None:
        """The tier that settled a claim."""
        self._tiers.labels(tier_decided=str(tier)).inc()

    def groundedness(self, score: float | None) -> None:
        """Score band."""
        self._bands.labels(band=score_band(score)).inc()

    def evidence_tier(self, tier: str) -> None:
        """Derived tier."""
        self._evidence.labels(tier=tier).inc()

    def tier0_ratio(self, ratio: float) -> None:
        """Tier 0 disagreement band."""
        self._tier0.labels(band=ratio_band(ratio)).inc()

    def failure_policy(self, action: str) -> None:
        """Block or flag."""
        self._policy.labels(action=action).inc()

    def completeness_missed(self, section_type: str | None, item: str) -> None:
        """A missed mandatory item (pack-declared section type and item)."""
        self._missed.labels(section_type=section_type or "other", item=item).inc()

    def gap_marker(self) -> None:
        """A gap claim."""
        self._gaps.inc()

    def tokens(self, task_class: str, model_profile: str, capability: str, count: int) -> None:
        """Tokens reported."""
        self._tokens.labels(
            task_class=task_class, model_profile=model_profile, capability=capability
        ).inc(count)


class LoggingGenerationObserver:
    """Logs what is worth a line (never content, OBS-2) and delegates to ``inner``."""

    def __init__(self, inner: GenerationObserver) -> None:
        self._inner = inner

    def span(self, name: str) -> AbstractAsyncContextManager[None]:
        """Delegates."""
        return self._inner.span(name)

    def first_frame(self, seconds: float, suppressed: bool) -> None:
        """Delegates."""
        self._inner.first_frame(seconds, suppressed)

    def verified(self, kind: str, seconds: float) -> None:
        """One line per verified generation: kind and duration."""
        log.info("generation_verified", kind=kind, seconds=round(seconds, 3))
        self._inner.verified(kind, seconds)

    def verdict_frame(self, delivered: bool) -> None:
        """A verdict frame the client never read leaves it with an unverified draft (GEN-1)."""
        if not delivered:
            log.warning("verdict_frame_not_delivered")
        self._inner.verdict_frame(delivered)

    def delivery(self, state: str) -> None:
        """A disconnect after dispatch still wrote the artefact (LD-18)."""
        if state == "disconnected":
            log.warning("client_disconnected_artefact_written")
        self._inner.delivery(state)

    def idempotent_replay(self) -> None:
        """A replay served from the stored artefact."""
        log.info("idempotent_replay")
        self._inner.idempotent_replay()

    def claim_outcome(self, outcome: str) -> None:
        """Delegates."""
        self._inner.claim_outcome(outcome)

    def tier_decided(self, tier: int) -> None:
        """Delegates."""
        self._inner.tier_decided(tier)

    def groundedness(self, score: float | None) -> None:
        """Delegates."""
        self._inner.groundedness(score)

    def evidence_tier(self, tier: str) -> None:
        """Delegates."""
        self._inner.evidence_tier(tier)

    def tier0_ratio(self, ratio: float) -> None:
        """Delegates."""
        self._inner.tier0_ratio(ratio)

    def failure_policy(self, action: str) -> None:
        """The pack's failure policy fired."""
        log.info("failure_policy_applied", action=action)
        self._inner.failure_policy(action)

    def completeness_missed(self, section_type: str | None, item: str) -> None:
        """Delegates."""
        self._inner.completeness_missed(section_type, item)

    def gap_marker(self) -> None:
        """Delegates."""
        self._inner.gap_marker()

    def tokens(self, task_class: str, model_profile: str, capability: str, count: int) -> None:
        """Delegates."""
        self._inner.tokens(task_class, model_profile, capability, count)


class TracingGenerationObserver(LoggingGenerationObserver):
    """Adds OpenTelemetry spans through the platform's tracer (§16.3).

    One span per pipeline stage under the request's trace; an escaping error is recorded by
    type only, never its message (``record_span_error``). Before tracing is installed the
    tracer records nothing.
    """

    def __init__(self, inner: GenerationObserver) -> None:
        super().__init__(inner)
        self._tracer = new_tracer("rag.generation")

    @asynccontextmanager
    async def span(self, name: str) -> AsyncIterator[None]:
        """A span named ``name`` around the block."""
        with self._tracer.start_as_current_span(name):
            try:
                yield
            except Exception as exc:
                record_span_error(exc, f"{name} failed")
                raise


def build_generation_observer(settings: Settings) -> GenerationObserver:
    """Structured logs always; Prometheus collectors when metrics are on and initialised."""
    inner: GenerationObserver = NullGenerationObserver()
    if settings.enable_metrics:
        try:
            inner = PrometheusGenerationObserver()
        except MetricsNotInitializedError:
            pass
    return TracingGenerationObserver(inner)
