"""``rag.engine``: the public, versioned API the Worker Pool imports (workers LLD §4, WB-1..8).

Nothing else in this distribution is public. The functions write the run's tables in one
transaction each, publish their event inside it and are idempotent (WB-8); the building blocks wrap
the code the API service's own routes use. ``llm_models.workers`` holds the plain types.

``ENGINE_API_VERSION`` follows semantic versioning (WB-2): a removal or a changed signature is a
major version, an addition a minor one. ``engine_api_v1.json`` is the recorded shape, compared by a
contract test here and in ``llm-workers`` (WB-4).
"""

from __future__ import annotations

from rag.engine.blocks import (
    Blocks,
    ModelAnswer,
    Retrieved,
    ScopeSnapshot,
    call_model,
    compose_prompt,
    gateway_context,
    load_pack,
    refine_instruction,
    reresolve,
    retrieve,
    run_extraction,
    translate,
    verify,
)
from rag.engine.bootstrap import Engine, build_engine
from rag.engine.context import EngineServices, RunContext, run_scope
from rag.engine.documents import (
    ConflictDraft,
    FactDraft,
    mint_document_version,
    record_checkpoint,
    record_coherence,
    record_facts,
)
from rag.engine.errors import (
    EngineError,
    InferenceUnavailable,
    NothingToMint,
    QuotaRefused,
    ResidencyRefused,
    RunGone,
    RunTerminal,
    SchemaViolation,
    ScopeRevoked,
)
from rag.engine.runs import begin_stage, complete_run, fail_run, run_state, suspend_for_quota
from rag.engine.sections import (
    GapDraft,
    SectionDraft,
    begin_section,
    fail_section,
    save_outline,
    save_section,
    save_section_gap,
)
from rag.infrastructure.persistence.generation.engine import OutlineEntry

ENGINE_API_VERSION = "1.0.0"

__all__ = [
    "ENGINE_API_VERSION",
    "Blocks",
    "ConflictDraft",
    "Engine",
    "EngineError",
    "EngineServices",
    "FactDraft",
    "GapDraft",
    "InferenceUnavailable",
    "ModelAnswer",
    "NothingToMint",
    "OutlineEntry",
    "QuotaRefused",
    "ResidencyRefused",
    "Retrieved",
    "RunContext",
    "RunGone",
    "RunTerminal",
    "SchemaViolation",
    "ScopeRevoked",
    "ScopeSnapshot",
    "SectionDraft",
    "begin_section",
    "begin_stage",
    "build_engine",
    "call_model",
    "complete_run",
    "compose_prompt",
    "fail_run",
    "fail_section",
    "gateway_context",
    "load_pack",
    "mint_document_version",
    "record_checkpoint",
    "record_coherence",
    "record_facts",
    "refine_instruction",
    "reresolve",
    "retrieve",
    "run_extraction",
    "run_scope",
    "run_state",
    "save_outline",
    "save_section",
    "save_section_gap",
    "suspend_for_quota",
    "translate",
    "verify",
]
