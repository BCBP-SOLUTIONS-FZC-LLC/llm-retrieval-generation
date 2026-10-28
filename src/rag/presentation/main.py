"""App factory and lifespan."""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import AsyncExitStack, asynccontextmanager

from eventcommon.inbox import apply_schema as apply_inbox_schema
from eventcommon.outbox import apply_schema
from fastapi import FastAPI
from fastapicommon import create_platform_app
from fastapicommon.health import ReadinessCheck, create_health_router
from pgcommon import MetricsIdentity, pgmetrics_init_with_identity

from rag.infrastructure.config import Settings, assert_isolation_config
from rag.infrastructure.persistence.pools import create_corpus_pool, create_generation_pool
from rag.presentation.api.internal import corpus as corpus_internal
from rag.presentation.api.internal import models as models_internal
from rag.presentation.api.operator import generations as operator_generations
from rag.presentation.api.public import artefacts, artefacts_retrieval_sets
from rag.presentation.api.public import corpus as corpus_public
from rag.presentation.api.public import generation as generation_public
from rag.presentation.api.public import regeneration as regeneration_public
from rag.presentation.api.public import retrieval as retrieval_public
from rag.presentation.api.public import workflows as workflows_public
from rag.presentation.api_docs import register_api_docs
from rag.presentation.dependencies import build_container
from rag.presentation.errors import register_error_handlers
from rag.presentation.events_dependencies import start_event_workers
from rag.presentation.identity import ExtraIdentityMiddleware

WorkerStop = Callable[[], Awaitable[None]]
WorkerStart = Callable[[FastAPI], Awaitable[WorkerStop]]


def register_background_worker(app: FastAPI, start: WorkerStart) -> None:
    """Register a worker for the lifespan to start once the pools are open.

    Workers stop in reverse order before the pools close. The events slice registers the
    outbox relay here (§18.3).
    """
    app.state.worker_starters.append(start)


def _lifespan(settings: Settings) -> Callable[[FastAPI], AsyncIterator[None]]:
    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        async with AsyncExitStack() as stack:
            if settings.enable_metrics:
                pgmetrics_init_with_identity(
                    MetricsIdentity(
                        settings.observability_domain, settings.app_name, settings.environment
                    )
                )
            corpus_pool = await create_corpus_pool(settings)
            stack.push_async_callback(corpus_pool.drain_and_close)
            generation_pool = await create_generation_pool(settings)
            stack.push_async_callback(generation_pool.drain_and_close)
            if settings.outbox_apply_schema_on_startup:
                await apply_schema(generation_pool)
                await apply_inbox_schema(generation_pool)  # the consumers' ledger lives here
                await apply_schema(corpus_pool)  # corpus events join llm_corpus transactions
            app.state.container = build_container(settings, corpus_pool, generation_pool)
            stack.push_async_callback(app.state.container.aclose)
            for start in app.state.worker_starters:
                stack.push_async_callback(await start(app))
            yield

    return lifespan  # type: ignore[return-value]


def _relay_ready(app: FastAPI) -> bool:
    """The outbox relays are polling (§18.1). Not ready before they started or after one stopped."""
    check = getattr(app.state, "relay_ready", None)
    return bool(check()) if callable(check) else False


def install_probes(app: FastAPI, settings: Settings) -> None:
    """Shadow the platform's check-less ``/ready`` with one that also gates on the relays.

    ``create_platform_app`` mounts a health router with no readiness checks and offers no hook for
    them. The same router, built with the check, is included and moved to the front of the route
    list, so it answers ``/health``, ``/live`` and ``/ready`` first (the first match wins).
    ``/live`` and ``/health`` behave as before: a stalled relay takes the pod out of rotation, not
    out of service (the relay recovers by itself once the database does).
    """
    checks: list[ReadinessCheck] = [lambda: _relay_ready(app)] if settings.events_enabled else []
    app.include_router(create_health_router(checks))
    app.router.routes.insert(0, app.router.routes.pop())


def create_app(settings: Settings | None = None) -> FastAPI:
    """Build the service app. Raises ``ConfigError`` when ``PG_BOUNCER_MODE`` is not ``true``."""
    settings = settings or Settings()
    assert_isolation_config(settings)
    app = create_platform_app(settings=settings, lifespan=_lifespan(settings))
    app.state.settings = settings
    app.state.worker_starters = []
    install_probes(app, settings)
    if settings.events_enabled:  # relays + consumers, in process (EVT-8, §18.3)
        register_background_worker(app, start_event_workers)
    app.add_middleware(ExtraIdentityMiddleware)
    register_error_handlers(app)
    app.include_router(artefacts.router)
    app.include_router(artefacts_retrieval_sets.router)
    app.include_router(retrieval_public.router)
    app.include_router(generation_public.router)
    app.include_router(workflows_public.router)
    app.include_router(regeneration_public.router)
    app.include_router(operator_generations.router)
    app.include_router(corpus_public.router)
    app.include_router(corpus_internal.router)
    app.include_router(models_internal.router)
    register_api_docs(app)
    return app
