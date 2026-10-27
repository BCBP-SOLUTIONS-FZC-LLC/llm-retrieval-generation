"""Structured logs of the retrieval pipeline (``fastapicommon.get_logger``, §15.1, OBS-2).

Tenant and request ids come from the request context the library binds. Never logged: the
query, chunk text, refinement content, ids as message text.
"""

from __future__ import annotations

from contextlib import AbstractAsyncContextManager

from fastapicommon.logging import get_logger

from rag.application.retrieval.ports import PipelineObserver

log = get_logger("rag.retrieval")


class LoggingObserver:
    """A ``PipelineObserver`` that logs the events worth a line and delegates to ``inner``."""

    def __init__(self, inner: PipelineObserver) -> None:
        self._inner = inner

    def stage(self, name: str) -> AbstractAsyncContextManager[None]:
        """Stage timing is a metric, not a log line."""
        return self._inner.stage(name)

    def marker(self, stage: str, reason: str) -> None:
        """A degradation (RET-1) is the customer-facing quality signal: one warning each."""
        log.warning("retrieval_degraded", stage=stage, reason=reason)
        self._inner.marker(stage, reason)

    def verdict(self, verdict: str, section_type: str | None) -> None:
        """The reduced verdict."""
        log.info("retrieval_verdict", verdict=verdict, section_type=section_type)
        self._inner.verdict(verdict, section_type)

    def rerank_skipped(self, reason: str) -> None:
        """Delegates (the marker already logged it)."""
        self._inner.rerank_skipped(reason)

    def candidate_depth(self, profile_source: str, depth: int) -> None:
        """Delegates."""
        self._inner.candidate_depth(profile_source, depth)

    def widened_retry(self) -> None:
        """D-16's single retry."""
        log.info("retrieval_widened_retry")
        self._inner.widened_retry()

    def required_input_gap(self, missing_role: str) -> None:
        """A customer action, distinct from insufficiency (§6.9)."""
        log.info("required_input_gap", missing_role=missing_role)
        self._inner.required_input_gap(missing_role)

    def scope_empty(self) -> None:
        """A spike is a misconfigured grant set or a probing caller (§16.2)."""
        log.warning("scope_resolution_empty")
        self._inner.scope_empty()

    def retrieval_cache(self, hit: bool) -> None:
        """Delegates."""
        self._inner.retrieval_cache(hit)

    def refiner(self, latency_seconds: float) -> None:
        """Delegates."""
        self._inner.refiner(latency_seconds)

    def instruction_refiner(self, latency_seconds: float) -> None:
        """Delegates."""
        self._inner.instruction_refiner(latency_seconds)
