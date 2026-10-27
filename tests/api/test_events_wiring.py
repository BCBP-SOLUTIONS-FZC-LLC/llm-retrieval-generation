"""``EVENTS_ENABLED`` registers the relays and consumers as one lifespan worker."""

from __future__ import annotations

from rag.infrastructure.config import Settings
from rag.presentation.events_dependencies import start_event_workers
from rag.presentation.main import create_app


def test_events_are_off_by_default_so_a_replica_without_aws_starts() -> None:
    app = create_app(Settings(environment="test", enable_tracing=False))
    assert app.state.worker_starters == []


def test_enabling_events_registers_one_worker() -> None:
    app = create_app(Settings(environment="test", enable_tracing=False, events_enabled=True))
    assert app.state.worker_starters == [start_event_workers]
