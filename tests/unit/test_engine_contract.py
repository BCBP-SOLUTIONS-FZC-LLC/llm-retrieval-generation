"""WB-4: the engine's public API is the recorded shape, and a shape change is a version change."""

from __future__ import annotations

import json
from pathlib import Path

import rag.engine as engine
from rag.engine.snapshot import snapshot

RECORDED = Path(engine.__file__).parent / "engine_api_v1.json"


def test_the_public_api_matches_the_recorded_snapshot() -> None:
    recorded = json.loads(RECORDED.read_text())
    assert snapshot() == recorded, (
        "the engine's public shape moved: bump ENGINE_API_VERSION (a removal or a changed "
        "signature is a major version, an addition a minor one) and regenerate "
        "`python -m rag.engine.snapshot > src/rag/engine/engine_api_v1.json`"
    )


def test_the_version_is_semantic_and_every_export_exists() -> None:
    major, minor, patch = engine.ENGINE_API_VERSION.split(".")
    assert (major, minor, patch) == ("1", "2", "0")  # v1 until a major change
    assert all(hasattr(engine, name) for name in engine.__all__)
    assert len(set(engine.__all__)) == len(engine.__all__)


def test_the_snapshot_covers_every_public_function_and_error() -> None:
    snap = snapshot()
    for name in (
        "begin_stage",
        "save_outline",
        "save_section",
        "mint_document_version",
        "run_scope",
    ):
        assert name in snap["functions"]
    for name in ("RunGone", "RunTerminal", "QuotaRefused", "ScopeRevoked", "SchemaViolation"):
        assert name in snap["errors"]
    assert "GenerationWorkflowInput" in snap["plain_types"]
