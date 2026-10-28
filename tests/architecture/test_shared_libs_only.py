"""Logs, metrics and traces reach the platform only through ``fastapicommon``.

No module imports a logging, metrics or tracing SDK directly, and none uses the standard
library's ``logging`` (``get_logger`` from ``fastapicommon.logging`` everywhere). Metrics and
tracing are built in ``infrastructure/observability`` from ``fastapicommon.metrics`` and
``fastapicommon.telemetry``; the application layer may import only the platform logger, and
``domain`` nothing from the platform. Third-party libraries (httpx, asyncpg, boto3) still log
through stdlib, and the platform's root handler renders those records too
(``tests/unit/test_logging_through_platform.py``).
"""

from __future__ import annotations

from tests.architecture.test_dependency_rules import (
    APPLICATION_PLATFORM_IMPORTS,
    imports,
    load_sources,
    under,
)

DIRECT_SDKS = ("logging", "structlog", "prometheus_client", "opentelemetry")


def test_no_module_imports_a_logging_metrics_or_tracing_sdk_directly() -> None:
    """Only ``fastapicommon`` (and ``pgcommon`` / ``eventcommon`` for their own metrics)."""
    offenders = [
        f"{module} imports {target}"
        for module, source in load_sources().items()
        for target in sorted(set(imports(module, source)))
        if any(under(target, sdk) for sdk in DIRECT_SDKS)
    ]
    assert offenders == []


def test_the_application_layer_imports_only_the_platform_logger() -> None:
    """Metrics and tracing are infrastructure; the application layer logs and nothing else."""
    offenders = [
        f"{module} imports {target}"
        for module, source in load_sources().items()
        if module.startswith("rag.application")
        for target in sorted(set(imports(module, source)))
        if under(target, "fastapicommon") and not under(target, APPLICATION_PLATFORM_IMPORTS)
    ]
    assert offenders == []


def test_the_domain_imports_nothing_from_the_platform() -> None:
    offenders = [
        f"{module} imports {target}"
        for module, source in load_sources().items()
        if module.startswith("rag.domain")
        for target in sorted(set(imports(module, source)))
        if under(target, "fastapicommon")
    ]
    assert offenders == []


def test_the_shared_observability_entry_points_are_used() -> None:
    """The collectors and tracers the service builds come from ``fastapicommon``."""
    sources = load_sources()
    for module in (
        "rag.infrastructure.observability.metrics",
        "rag.infrastructure.observability.generation_metrics",
        "rag.infrastructure.observability.workflow_metrics",
    ):
        assert any(
            t.startswith("fastapicommon.metrics") for t in imports(module, sources[module])
        ), module
    for module in (
        "rag.infrastructure.observability.generation_metrics",
        "rag.infrastructure.observability.workflow_metrics",
    ):
        assert any(
            t.startswith("fastapicommon.telemetry") for t in imports(module, sources[module])
        ), module


def test_every_module_that_logs_does_so_through_get_logger() -> None:
    """A module-level ``log`` is built by ``get_logger``, never by a stdlib ``getLogger``."""
    offenders = [
        module
        for module, source in load_sources().items()
        if "getLogger(" in source or "basicConfig(" in source
    ]
    assert offenders == []
