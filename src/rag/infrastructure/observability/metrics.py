"""§16.2 pipeline metrics, Tier 3 (``llm_retrieval_generation_<metric>``), OBS-1/OBS-3.

Collectors are built only when metrics are enabled (``ENABLE_METRICS=true`` with
``OBSERVABILITY_DOMAIN=llm``, registered from ``fastapicommon`` 0.6.0, OQ-103; otherwise the
observer is a no-op).
Labels are bounded vocabularies: stage names, marker reasons, verdicts, pack-declared section
types (anything else is ``other``), pack roles. Never a tenant, user, query or id.

Deviations from the §16.2 table, forced by OBS-3: ``candidate_depth`` is a counter per band
(``candidate_depth_band_total``) and ``retrieval_cache_hit_ratio`` is a gauge.
"""

from __future__ import annotations

import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapicommon.metrics import (
    MetricsNotInitializedError,
    new_counter,
    new_gauge,
    new_histogram,
)

from rag.application.retrieval.ports import NullObserver, PipelineObserver
from rag.infrastructure.config import Settings
from rag.infrastructure.observability.logs import LoggingObserver

P = "llm_retrieval_generation_"
STAGES = frozenset(
    {"scope", "pack", "refine", "plan", "embed", "search", "fusion", "rerank", "assembly"}
)
DEPTH_BANDS = ((8, "1-8"), (16, "9-16"), (24, "17-24"), (48, "25-48"))

METRIC_NAMES = {
    "counter": [
        "degradation_marker_total",
        "sufficiency_verdict_total",
        "required_input_gap_total",
        "rerank_skipped_total",
        "scope_resolution_empty_total",
        "candidate_depth_band_total",
        "retrieval_verdict_total",
        "widened_retry_total",
    ],
    "histogram": ["retrieval_stage_duration_seconds", "refiner_latency_seconds"],
    "gauge": ["retrieval_cache_hit_ratio"],
}
"""Every collector name (without the prefix), so a test can validate them without a registry."""


def depth_band(depth: int) -> str:
    """The counter-per-band form of a candidate depth (OBS-3)."""
    return next((label for top, label in DEPTH_BANDS if depth <= top), "49+")


class PrometheusObserver:
    """``PipelineObserver`` over ``fastapicommon`` Tier-3 collectors."""

    def __init__(self) -> None:
        self._stage = new_histogram(
            P + "retrieval_stage_duration_seconds", "Retrieval stage duration", ["stage"]
        )
        self._markers = new_counter(
            P + "degradation_marker_total", "Degradation markers", ["stage", "reason"]
        )
        self._sufficiency = new_counter(
            P + "sufficiency_verdict_total", "Sufficiency verdicts", ["verdict", "section_type"]
        )
        self._gaps = new_counter(
            P + "required_input_gap_total", "Missing required roles", ["missing_role"]
        )
        self._rerank_skipped = new_counter(
            P + "rerank_skipped_total", "Rerank skipped or failed", ["reason"]
        )
        self._scope_empty = new_counter(P + "scope_resolution_empty_total", "Empty scope")
        self._depth = new_counter(
            P + "candidate_depth_band_total", "Candidate depth per band", ["profile_source", "band"]
        )
        self._verdicts = new_counter(
            P + "retrieval_verdict_total", "Retrieval verdicts", ["verdict"]
        )
        self._widened = new_counter(P + "widened_retry_total", "Widened retries")
        self._refiner = new_histogram(P + "refiner_latency_seconds", "Refiner latency", ["refiner"])
        self._cache = new_gauge(P + "retrieval_cache_hit_ratio", "Retrieval-set cache hit ratio")
        self._hits = self._lookups = 0

    @asynccontextmanager
    async def stage(self, name: str) -> AsyncIterator[None]:
        """Observe the stage's wall time (unknown names collapse to ``other``)."""
        started = time.monotonic()
        try:
            yield
        finally:
            label = name if name in STAGES else "other"
            self._stage.labels(stage=label).observe(time.monotonic() - started)

    def marker(self, stage: str, reason: str) -> None:
        """One RET-1 marker."""
        self._markers.labels(stage=stage, reason=reason).inc()

    def verdict(self, verdict: str, section_type: str | None) -> None:
        """Verdict counters."""
        self._verdicts.labels(verdict=verdict).inc()
        self._sufficiency.labels(verdict=verdict, section_type=section_type or "other").inc()

    def rerank_skipped(self, reason: str) -> None:
        """Rerank skip."""
        self._rerank_skipped.labels(reason=reason).inc()

    def candidate_depth(self, profile_source: str, depth: int) -> None:
        """Depth band."""
        self._depth.labels(profile_source=profile_source, band=depth_band(depth)).inc()

    def widened_retry(self) -> None:
        """Widened retry."""
        self._widened.inc()

    def required_input_gap(self, missing_role: str) -> None:
        """A missing role."""
        self._gaps.labels(missing_role=missing_role).inc()

    def scope_empty(self) -> None:
        """An empty scope."""
        self._scope_empty.inc()

    def retrieval_cache(self, hit: bool) -> None:
        """Maintain the running hit ratio."""
        self._lookups += 1
        self._hits += int(hit)
        self._cache.set(self._hits / self._lookups)

    def refiner(self, latency_seconds: float) -> None:
        """A query-refiner call's latency."""
        self._refiner.labels(refiner="query").observe(latency_seconds)

    def instruction_refiner(self, latency_seconds: float) -> None:
        """An Instruction Refiner call's latency (§16.2, the 3 s budget)."""
        self._refiner.labels(refiner="instruction").observe(latency_seconds)


def build_observer(settings: Settings) -> PipelineObserver:
    """Structured logs always; Prometheus collectors when metrics are on and initialised."""
    inner: PipelineObserver = NullObserver()
    if settings.enable_metrics:
        try:
            inner = PrometheusObserver()
        except MetricsNotInitializedError:
            pass
    return LoggingObserver(inner)
