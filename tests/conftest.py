"""Shared fixtures."""

from __future__ import annotations

import pytest

from rag.infrastructure.config import Settings


@pytest.fixture(autouse=True)
def _ignore_dotenv(monkeypatch: pytest.MonkeyPatch) -> None:
    """A developer's ``.env`` (copied from ``.env-example`` by ``make run``) must not leak in."""
    monkeypatch.setitem(Settings.model_config, "env_file", None)


@pytest.fixture
def settings() -> Settings:
    """Settings with tracing and metrics off, for tests that build the app."""
    return Settings(environment="test", enable_tracing=False, enable_metrics=False)
