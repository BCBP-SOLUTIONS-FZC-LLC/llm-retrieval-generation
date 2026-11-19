"""The composition root of the engine: pools, clients and breakers without an app or a lifespan.

The Worker Pool calls ``build_engine`` once at startup and ``Engine.aclose`` at shutdown. It wires
the same objects the API service wires (``build_container``), so a worker and a request handler
share one resolver, one pipeline, one verifier and one Gateway client. "No FastAPI" means no
application and no lifespan; the distribution still depends on the platform libraries (logging
and metrics go through ``fastapicommon``, as everywhere in this service).
"""

from __future__ import annotations

from collections.abc import Mapping
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from typing import Any

from pgcommon import Pool

from rag.application.generation.clock import SystemClock
from rag.application.generation.commands import AppendAuditEntry
from rag.application.generation.refiner import InstructionRefiner
from rag.application.workflows.ports import PrincipalDirectory
from rag.engine.blocks import Blocks
from rag.engine.context import EngineServices, RunContext, run_scope
from rag.infrastructure.config import Settings, assert_isolation_config
from rag.infrastructure.events.publisher import OutboxEventPublisher
from rag.infrastructure.iam.principals import HttpPrincipalDirectory, UnavailablePrincipalDirectory
from rag.infrastructure.persistence.generation.engine import PgEngineRepository
from rag.infrastructure.persistence.generation.regeneration import PgFactRepository
from rag.infrastructure.persistence.generation.repositories import PgAuditChainRepository
from rag.infrastructure.persistence.generation.workflows import PgRunRepository
from rag.infrastructure.persistence.generation.writes import PgGenerationWriteRepository
from rag.infrastructure.persistence.pools import create_corpus_pool, create_generation_pool
from rag.infrastructure.storage.artefact_content_store import S3ArtefactContentStore
from rag.presentation.dependencies import Container, build_container


@dataclass(slots=True)
class Engine:
    """The built engine: its services, and what closes when the worker stops."""

    services: EngineServices
    container: Container
    pools: tuple[Pool, ...]

    def run_scope(self, run_input: Mapping[str, Any]) -> AbstractAsyncContextManager[RunContext]:
        """One activity's scope: the tenant bound, the start argument decoded."""
        return run_scope(self.services, run_input)

    async def aclose(self) -> None:
        """Drain the clients the container owns, then close both pools."""
        await self.container.aclose()
        for pool in self.pools:
            await pool.drain_and_close()


async def build_engine(
    settings: Settings, *, directory: PrincipalDirectory | None = None
) -> Engine:
    """Open the two pools and wire the engine.

    ``GENERATION_DATABASE_URL`` must log in as a member of ``rag_activity`` and
    ``CORPUS_DATABASE_URL`` as one that may read ``collections`` and ``index_generations``. The
    isolation check is the API's: ``PG_BOUNCER_MODE`` is exactly ``true``, or the tenant GUC would
    leak between clients.
    """
    assert_isolation_config(settings)
    corpus_pool = await create_corpus_pool(settings)
    generation_pool = await create_generation_pool(settings)
    container = build_container(settings, corpus_pool, generation_pool)
    retrieval, generate = container.retrieval, container.generate
    iam = directory or (
        HttpPrincipalDirectory(settings.iam_base_url)
        if settings.iam_base_url
        else UnavailablePrincipalDirectory()
    )
    blocks = Blocks(
        pipeline=retrieval.pipeline,
        recorder=retrieval.recorder,
        scope=container.scope.adapter,
        packs=retrieval.packs,
        gateway=retrieval.gateway,
        directory=iam,
        verifier=generate.verify,
        refiner=InstructionRefiner(
            retrieval.gateway,
            enabled=settings.refiner_instruction_enabled,
            deadline_seconds=settings.refiner_instruction_deadline_ms / 1000,
            observer=retrieval.observer,
        ),
        extractor=generate.extract,
        admit=generate.admit,
    )
    services = EngineServices(
        tx=container.generation_tx,
        runs=PgRunRepository(),
        writes=PgGenerationWriteRepository(),
        facts=PgFactRepository(),
        engine=PgEngineRepository(),
        events=OutboxEventPublisher(),
        audit=AppendAuditEntry(PgAuditChainRepository()),
        clock=SystemClock(),
        store=S3ArtefactContentStore(
            settings.s3_bucket,
            region=settings.s3_region,
            endpoint_url=settings.s3_endpoint_url,
            kms_key_id=settings.s3_kms_key_id,
        ),
        review_gate_days=settings.review_gate_days,
        blocks=blocks,
    )
    return Engine(services, container, (corpus_pool, generation_pool))
