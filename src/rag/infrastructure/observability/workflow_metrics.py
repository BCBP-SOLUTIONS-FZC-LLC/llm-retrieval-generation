"""§16.2 async-run metrics, OBS-1 to OBS-3.

``scope_revoked_async_total`` and ``regeneration_total`` are the LLD's; ``workflow_start_total``,
``workflow_signal_total``, ``gate_action_total`` and ``fact_conflict_resolution_total`` are not
named by it (design additions, CFG-1).

Built only when metrics are enabled (the platform registry has no ``llm`` domain yet, OQ-103).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from typing import Any

from fastapicommon.logging import get_logger
from fastapicommon.metrics import MetricsNotInitializedError, new_counter
from fastapicommon.telemetry import new_tracer, record_span_error

from rag.application.workflows.observability import NullWorkflowObserver, WorkflowObserver
from rag.infrastructure.config import Settings

P = "llm_retrieval_generation_"
log = get_logger("rag.workflows")

METRIC_NAMES = {
    "counter": [
        "scope_revoked_async_total",
        "regeneration_total",
        "workflow_start_total",
        "workflow_signal_total",
        "gate_action_total",
        "fact_conflict_resolution_total",
    ],
    "histogram": [],
    "gauge": [],
}
"""Every collector name (without the prefix), so a test can validate them without a registry."""

LABELS = {
    "regeneration_total": ["scope", "origin"],
    "workflow_start_total": ["outcome"],
    "workflow_signal_total": ["signal", "outcome"],
    "gate_action_total": ["action"],
    "fact_conflict_resolution_total": ["resolution"],
}


class PrometheusWorkflowObserver:
    """``WorkflowObserver`` over ``fastapicommon`` Tier-3 collectors."""

    def __init__(self) -> None:
        def counter(name: str, doc: str) -> Any:
            return new_counter(P + name, doc, LABELS.get(name, []))

        self._revoked = counter("scope_revoked_async_total", "Runs ended by scope re-resolution")
        self._regen = counter("regeneration_total", "Regenerations admitted")
        self._start = counter("workflow_start_total", "Workflow starts")
        self._signal = counter("workflow_signal_total", "Workflow signals")
        self._gate = counter("gate_action_total", "Gate and resume actions accepted")
        self._conflict = counter("fact_conflict_resolution_total", "Fact conflicts resolved")

    @asynccontextmanager
    async def span(self, name: str) -> AsyncIterator[None]:
        """Spans are the tracing wrapper's job."""
        yield

    def workflow_start(self, outcome: str) -> None:
        """Start outcome."""
        self._start.labels(outcome=outcome).inc()

    def workflow_signal(self, signal: str, outcome: str) -> None:
        """Signal outcome."""
        self._signal.labels(signal=signal, outcome=outcome).inc()

    def gate_action(self, action: str) -> None:
        """An accepted gate action."""
        self._gate.labels(action=action).inc()

    def scope_revoked(self) -> None:
        """A run ended scope_revoked."""
        self._revoked.inc()

    def regeneration(self, scope: str, origin: str) -> None:
        """A regeneration admitted."""
        self._regen.labels(scope=scope, origin=origin).inc()

    def fact_conflict(self, resolution: str) -> None:
        """A conflict resolved."""
        self._conflict.labels(resolution=resolution).inc()


class TracingWorkflowObserver:
    """Logs what is worth a line (never content, OBS-2), adds spans, delegates to ``inner``."""

    def __init__(self, inner: WorkflowObserver) -> None:
        self._inner = inner
        self._tracer = new_tracer("rag.workflows")

    def span(self, name: str) -> AbstractAsyncContextManager[None]:
        """A span named ``name`` around the block; an escaping error is recorded by type only."""

        @asynccontextmanager
        async def run() -> AsyncIterator[None]:
            with self._tracer.start_as_current_span(name):
                try:
                    yield
                except Exception as exc:
                    record_span_error(exc, f"{name} failed")
                    raise

        return run()

    def workflow_start(self, outcome: str) -> None:
        """A failed start leaves a recoverable ``pending`` row (ASYNC-2): worth a warning."""
        (log.warning if outcome == "failed" else log.info)("workflow_start", outcome=outcome)
        self._inner.workflow_start(outcome)

    def workflow_signal(self, signal: str, outcome: str) -> None:
        """A signal that failed after commit strands the run until it is repeated."""
        (log.warning if outcome == "failed" else log.info)(
            "workflow_signal", signal=signal, outcome=outcome
        )
        self._inner.workflow_signal(signal, outcome)

    def gate_action(self, action: str) -> None:
        """Delegates."""
        self._inner.gate_action(action)

    def scope_revoked(self) -> None:
        """The one a human must see (§6.3)."""
        log.warning("run_scope_revoked")
        self._inner.scope_revoked()

    def regeneration(self, scope: str, origin: str) -> None:
        """Delegates."""
        self._inner.regeneration(scope, origin)

    def fact_conflict(self, resolution: str) -> None:
        """Delegates."""
        log.info("fact_conflict_resolved", resolution=resolution)
        self._inner.fact_conflict(resolution)


def build_workflow_observer(settings: Settings) -> WorkflowObserver:
    """Structured logs always; Prometheus collectors when metrics are on and initialised."""
    inner: WorkflowObserver = NullWorkflowObserver()
    if settings.enable_metrics:
        try:
            inner = PrometheusWorkflowObserver()
        except MetricsNotInitializedError:
            pass
    return TracingWorkflowObserver(inner)
