"""The engine's metric prefix follows the caller's identity (engine 1.2.0, ADR-0003 gap 6)."""

from types import SimpleNamespace

import pytest

from rag.infrastructure.observability import prefix


def test_the_api_keeps_its_own_prefix_before_and_after_init(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(prefix, "metrics_identity", lambda: None)
    assert prefix.metric_prefix() == "llm_retrieval_generation_"
    api = SimpleNamespace(domain="llm", service="llm-retrieval-generation")
    monkeypatch.setattr(prefix, "metrics_identity", lambda: api)
    assert prefix.metric_prefix() == prefix.API_PREFIX  # byte-identical to the old constant


def test_a_worker_process_gets_its_own_prefix(monkeypatch: pytest.MonkeyPatch) -> None:
    workers = SimpleNamespace(domain="llm", service="llm-workers")
    monkeypatch.setattr(prefix, "metrics_identity", lambda: workers)
    assert prefix.metric_prefix() == "llm_workers_"
