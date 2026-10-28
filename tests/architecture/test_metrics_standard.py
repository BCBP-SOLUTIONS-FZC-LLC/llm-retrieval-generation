"""Enterprise Platform Observability Standard, enforced over every collector this service declares.

Classification (tier), naming, suffixes, label vocabulary and registry compliance are checked
here in one place, across the three observability modules, and the documented inventory must
match the code both ways. Tier 1 (``platform_*``) and Tier 2 (``<domain>_*``) metrics are
registry-owned: this service never declares one, it only emits what the platform libraries do.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from fastapicommon.metrics import default_registry
from fastapicommon.metrics.registry import suffix_error
from tests.architecture.test_dependency_rules import load_sources

from rag.infrastructure.observability import generation_metrics, metrics, workflow_metrics

DOMAIN, SERVICE = "llm", "retrieval-generation"
PREFIX = "llm_retrieval_generation_"
MODULES = (metrics, generation_metrics, workflow_metrics)
FORBIDDEN_LABELS = {"user_id", "email", "tenant_id", "request_id", "event_id", "session_id"}
CENTRAL_LABELS = {"domain", "service", "environment"}
INVENTORY = Path(__file__).resolve().parents[2] / "docs/observability/metric-registry.md"
COLLECTOR_FACTORIES = {"new_counter", "new_histogram", "new_gauge"}
SHARED_FACTORIES = {"new_shared_counter", "new_shared_histogram", "new_shared_gauge"}


def declared() -> list[tuple[str, str, list[str]]]:
    """(kind, full name, labels) for every collector in the three modules."""
    out: list[tuple[str, str, list[str]]] = []
    for module in MODULES:
        labels = getattr(module, "LABELS", {})
        for kind, names in module.METRIC_NAMES.items():
            out += [(kind, PREFIX + n, list(labels.get(n, []))) for n in names]
    return out


def test_every_collector_is_tier_3_named_domain_service_metric() -> None:
    """Decision tree, last branch: nothing here is platform_* or a domain-shared name."""
    names = [n for _, n, _ in declared()]
    assert names and all(n.startswith(PREFIX) for n in names)
    assert not [n for n in names if n.startswith("platform_")]
    assert len(names) == len(set(names)), "a metric declared twice"


@pytest.mark.parametrize(("kind", "name", "labels"), declared())
def test_naming_suffix_and_label_rules(kind: str, name: str, labels: list[str]) -> None:
    assert suffix_error(kind, name, "") == ""  # counters _total, histograms _seconds
    assert re.fullmatch(r"[a-z][a-z0-9_]*", name)
    assert not set(labels) & FORBIDDEN_LABELS  # high-cardinality labels are prohibited
    assert not set(labels) & CENTRAL_LABELS  # injected centrally, never declared
    default_registry().check_custom(kind, name, labels, DOMAIN, SERVICE)  # registry rules


def test_the_service_never_declares_shared_metrics_or_its_own_registerer() -> None:
    """Shared metrics come from the registry (``new_shared_*``); none is claimed here, and no
    collector is built outside ``infrastructure/observability``."""
    offenders = []
    for module, source in load_sources().items():
        used = {w for w in COLLECTOR_FACTORIES | SHARED_FACTORIES if re.search(rf"\b{w}\(", source)}
        if used and not module.startswith("rag.infrastructure.observability"):
            offenders.append(f"{module}: {sorted(used)}")
        if used & SHARED_FACTORIES:
            offenders.append(f"{module} claims a shared metric: {sorted(used & SHARED_FACTORIES)}")
    assert offenders == []


def test_no_metric_name_literal_outside_the_prefix_in_source() -> None:
    """A stray ``platform_*`` or other metric-looking name would be a unilateral invention."""
    stray = [
        f"{module}: {m}"
        for module, source in load_sources().items()
        for m in re.findall(r'"(platform_[a-z0-9_]+_(?:total|seconds))"', source)
    ]
    assert stray == []


def test_the_inventory_document_matches_the_code_both_ways() -> None:
    rows = re.findall(
        r"^\| `([a-z0-9_]+)` \| (counter|histogram|gauge) \|", INVENTORY.read_text(), re.M
    )
    documented = {(kind, PREFIX + name) for name, kind in rows}
    in_code = {(kind, name) for kind, name, _ in declared()}
    assert in_code - documented == set(), (
        "declared but not in docs/observability/metric-registry.md"
    )
    assert documented - in_code == set(), "documented but not declared"
