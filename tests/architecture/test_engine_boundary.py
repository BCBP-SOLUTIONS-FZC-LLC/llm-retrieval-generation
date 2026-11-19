"""WB-1, WB-7: ``rag.engine`` is a composition root and the Worker Pool's only door in.

Nothing in the service imports it, it never imports Temporal, only its bootstrap reaches the
presentation layer, and the plain types stay standard library only.
"""

from __future__ import annotations

from tests.architecture.test_dependency_rules import imports, load_sources


def _engine(sources: dict[str, str]) -> dict[str, str]:
    return {m: s for m, s in sources.items() if m == "rag.engine" or m.startswith("rag.engine.")}


def test_nothing_in_the_service_imports_the_engine() -> None:
    sources = load_sources()
    offenders = [
        m
        for m, src in sources.items()
        if not m.startswith("rag.engine")
        and any(i.startswith("rag.engine") for i in imports(m, src))
    ]
    assert offenders == []


def test_the_engine_never_imports_temporal_wb7() -> None:
    offenders = [
        (m, i)
        for m, src in _engine(load_sources()).items()
        for i in imports(m, src)
        if i.split(".")[0] == "temporalio"
    ]
    assert offenders == []


def test_only_the_bootstrap_reaches_the_presentation_layer() -> None:
    offenders = [
        m
        for m, src in _engine(load_sources()).items()
        if m != "rag.engine.bootstrap"
        and any(i.startswith("rag.presentation") for i in imports(m, src))
    ]
    assert offenders == []


def test_the_engine_writes_only_through_the_repositories_and_the_publisher() -> None:
    """No driver and no SQL in the engine's modules: every statement lives in a repository."""
    for m, src in _engine(load_sources()).items():
        assert "tx.execute(" not in src and "tx.fetch" not in src, m
