"""The engine's collaborators and the per-activity context (workers LLD §4.2, ``run_scope``)."""

from __future__ import annotations

from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from llm_models.workers import GenerationWorkflowInput
from rag.application.generation.commands import AppendAuditEntry
from rag.application.generation.ports import ArtefactContentStore
from rag.application.shared.events import EventPublisher
from rag.application.shared.ports import TransactionRunner
from rag.domain.generation.repositories import Clock, FactRepository, GenerationWriteRepository
from rag.domain.workflows.repositories import RunRepository
from rag.infrastructure.persistence.generation.engine import PgEngineRepository
from rag.infrastructure.persistence.pools import bind_tenant_scope


@dataclass(frozen=True, slots=True)
class EngineServices:
    """Everything the write functions need. Built once, by ``rag.engine.bootstrap``."""

    tx: TransactionRunner
    runs: RunRepository
    writes: GenerationWriteRepository
    facts: FactRepository
    engine: PgEngineRepository
    events: EventPublisher
    audit: AppendAuditEntry
    clock: Clock
    store: ArtefactContentStore
    review_gate_days: int = 30
    blocks: Any = None
    """The model-calling and retrieval building blocks (``rag.engine.blocks.Blocks``)."""


@dataclass(frozen=True, slots=True)
class RunContext:
    """One activity's view of its run: the services and the decoded start argument."""

    services: EngineServices
    input: GenerationWorkflowInput

    @property
    def run_id(self) -> UUID:
        """The generation run."""
        return UUID(self.input.generation_run_id)

    @property
    def tenant_id(self) -> UUID:
        """The tenant every statement is scoped to."""
        return UUID(self.input.tenant_id)

    @property
    def principal_id(self) -> UUID:
        """The principal the run acts for."""
        return UUID(self.input.principal_id)


@asynccontextmanager
async def run_scope(
    services: EngineServices, run_input: Mapping[str, Any]
) -> AsyncIterator[RunContext]:
    """Bind the tenant for every pool statement in the block and yield the run's context.

    It sets context and holds no connection: the pool binds the GUC per transaction, so nothing is
    pinned across a model call (PgBouncer transaction mode, workers LLD G-11, WT-3).
    """
    inp = GenerationWorkflowInput.from_dict(run_input)
    with bind_tenant_scope(UUID(inp.tenant_id)):
        yield RunContext(services, inp)
