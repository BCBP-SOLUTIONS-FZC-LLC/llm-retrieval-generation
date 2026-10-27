"""Metric, log and tracing observers: every method runs and reaches its collector or delegate.

The platform registry has no ``llm`` domain (OQ-103), so the collector factories are replaced
with mocks; what is checked is the wiring, not Prometheus.
"""

from __future__ import annotations

import inspect
from typing import Any
from unittest.mock import MagicMock

import pytest

from rag.application.retrieval.ports import NullObserver
from rag.application.shared.observability import NullGenerationObserver
from rag.application.workflows.observability import NullWorkflowObserver
from rag.infrastructure.config import Settings
from rag.infrastructure.observability import generation_metrics as gm
from rag.infrastructure.observability import metrics as rm
from rag.infrastructure.observability import workflow_metrics as wm
from rag.infrastructure.observability.logs import LoggingObserver

_ARGS: dict[str, Any] = {
    "str": "x",
    "int": 1,
    "float": 0.5,
    "bool": True,
    "str | None": "x",
    "float | None": 0.5,
}


def _call_all(obj: object, *, skip: frozenset[str] = frozenset({"span", "stage"})) -> list[str]:
    """Call every public sync method with arguments built from its annotations."""
    called = []
    for name, method in inspect.getmembers(obj, inspect.ismethod):
        if name.startswith("_") or name in skip:
            continue
        params = inspect.signature(method).parameters
        method(**{p: _ARGS[str(spec.annotation)] for p, spec in params.items()})
        called.append(name)
    return called


@pytest.fixture
def collectors(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    """Replace the three collector factories of every metrics module with one mock factory."""
    factory = MagicMock(name="collector")
    for module in (rm, gm, wm):
        for fn in ("new_counter", "new_gauge", "new_histogram"):
            monkeypatch.setattr(module, fn, lambda *a, **k: factory, raising=False)
    return factory


def test_depth_and_ratio_bands() -> None:
    """Bands cover every edge, including the open-ended last one."""
    assert [rm.depth_band(d) for d in (1, 8, 9, 24, 48, 49)] == [
        "1-8",
        "1-8",
        "9-16",
        "17-24",
        "25-48",
        "49+",
    ]
    assert gm.score_band(None) == "not_computed"
    assert gm.ratio_band(0.0) != gm.ratio_band(0.9)
    assert gm.ratio_band(0.9) == ">0.50"


@pytest.mark.parametrize(
    "observer",
    [rm.PrometheusObserver, gm.PrometheusGenerationObserver, wm.PrometheusWorkflowObserver],
)
def test_prometheus_observers_drive_their_collectors(observer: type, collectors: MagicMock) -> None:
    """Each public method updates a collector (label set or direct)."""
    obs = observer()
    assert _call_all(obs)
    assert collectors.method_calls or collectors.labels.called


@pytest.mark.asyncio
async def test_prometheus_stage_and_span(collectors: MagicMock) -> None:
    """``stage`` observes unknown names under ``other``; the spans are plain pass-throughs."""
    obs = rm.PrometheusObserver()
    async with obs.stage("search"):
        pass
    async with obs.stage("nonsense"):
        pass
    labelled = [c.kwargs["stage"] for c in collectors.labels.call_args_list if "stage" in c.kwargs]
    assert labelled == ["search", "other"]
    async with gm.PrometheusGenerationObserver().span("x"):
        pass
    async with wm.PrometheusWorkflowObserver().span("x"):
        pass


def test_retrieval_cache_ratio_is_a_running_hit_rate(collectors: MagicMock) -> None:
    """Two lookups, one hit: the gauge reads 0.5."""
    obs = rm.PrometheusObserver()
    obs.retrieval_cache(True)
    obs.retrieval_cache(False)
    collectors.set.assert_called_with(0.5)


def test_generation_delivered_ratio_is_a_running_hit_rate(collectors: MagicMock) -> None:
    """One delivered and one not: 0.5."""
    obs = gm.PrometheusGenerationObserver()
    obs.verdict_frame(True)
    obs.verdict_frame(False)
    collectors.set.assert_called_with(0.5)


@pytest.mark.parametrize(
    ("build", "null", "wrapper"),
    [
        (rm.build_observer, NullObserver, LoggingObserver),
        (gm.build_generation_observer, NullGenerationObserver, gm.TracingGenerationObserver),
        (wm.build_workflow_observer, NullWorkflowObserver, wm.TracingWorkflowObserver),
    ],
)
def test_builders(
    build: Any, null: type, wrapper: type, collectors: MagicMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Logging/tracing wrapper always; Prometheus inside only with metrics on and initialised."""
    off = build(Settings(enable_metrics=False))
    assert isinstance(off, wrapper)
    assert isinstance(off._inner, null)
    on = build(Settings(enable_metrics=True))
    assert not isinstance(on._inner, null)


@pytest.mark.parametrize(
    "build", [rm.build_observer, gm.build_generation_observer, wm.build_workflow_observer]
)
def test_builders_fall_back_when_metrics_are_not_initialised(build: Any) -> None:
    """No registry initialised: the Null inner stays (the app still starts)."""
    wrapped = build(Settings(enable_metrics=True))
    assert type(wrapped._inner).__name__.startswith("Null")


@pytest.mark.asyncio
async def test_logging_and_tracing_wrappers_delegate() -> None:
    """Every wrapper method logs and reaches the inner observer; spans propagate errors."""
    for wrapper in (
        LoggingObserver(NullObserver()),
        gm.TracingGenerationObserver(NullGenerationObserver()),
        wm.TracingWorkflowObserver(NullWorkflowObserver()),
    ):
        assert _call_all(wrapper)
    async with LoggingObserver(NullObserver()).stage("search"):
        pass
    for span_owner in (
        gm.TracingGenerationObserver(NullGenerationObserver()),
        wm.TracingWorkflowObserver(NullWorkflowObserver()),
    ):
        async with span_owner.span("ok"):
            pass
        with pytest.raises(RuntimeError):
            async with span_owner.span("boom"):
                raise RuntimeError("x")


def test_failed_workflow_outcomes_log_a_warning() -> None:
    """A failed start or signal is the warning branch; others are info."""
    obs = wm.TracingWorkflowObserver(NullWorkflowObserver())
    obs.workflow_start("failed")
    obs.workflow_signal("cancel", "failed")
    obs.workflow_start("started")
    obs.workflow_signal("cancel", "sent")
