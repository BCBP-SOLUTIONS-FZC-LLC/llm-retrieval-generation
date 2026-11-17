"""App factory and lifespan."""

from __future__ import annotations

import asyncio
import contextvars
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import AsyncExitStack, asynccontextmanager
from typing import Any

from eventcommon.inbox import apply_schema as apply_inbox_schema
from eventcommon.outbox import apply_schema
from fastapi import FastAPI
from fastapicommon import create_platform_app
from fastapicommon.health import ReadinessCheck, create_health_router
from fastapicommon.logging import get_logger
from pgcommon import MetricsIdentity, pgmetrics_init_with_identity

from rag.infrastructure.config import Settings, assert_isolation_config
from rag.infrastructure.persistence.generation.probes import PgStoreProbe
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


READINESS_DB_TIMEOUT_SECONDS = 0.6
"""Both checks run at once, so a hung database answers not ready in under a probe's usual second."""
READINESS_CACHE_SECONDS = 1.0
"""A poll inside this window gets the last answer: /ready is unauthenticated by design."""
log = get_logger("rag.readiness")


def _databases_ready(app: FastAPI) -> Callable[[], Awaitable[bool]]:
    """Both databases take a write transaction on their primary (§18, GEN-3, SCOPE-10).

    A replica that cannot record an artefact or resolve a grant must not take traffic. The check
    is the GEN-3 probe on each pool: it opens a transaction and requires ``transaction_read_only``
    to be off, so a read-only standby fails it too. A failure is logged when the state changes,
    not on every poll.
    """
    last: list[bool | None] = [None]
    cached: list[float | bool] = [float("-inf"), False]  # when, answer

    lock = asyncio.Lock()

    def fresh() -> bool:
        return time.monotonic() - float(cached[0]) < READINESS_CACHE_SECONDS

    async def check() -> bool:
        if fresh():
            return bool(cached[1])  # /ready is unauthenticated: bound the transactions it can open
        async with lock:  # one probe at a time; callers that waited take its answer
            if fresh():
                return bool(cached[1])
            # An empty context: the probe must not run under the caller's identity headers (an
            # unauthenticated request with only x-user-id made the GUC provider refuse, so it
            # read as "not ready" for every caller for a second).
            answer = await asyncio.create_task(probe(), context=contextvars.Context())
            cached[0], cached[1] = time.monotonic(), answer
            return answer

    async def probe() -> bool:
        container = getattr(app.state, "container", None)
        failed: list[str] = []
        causes: dict[str, str] = {}
        if container is None:
            failed.append("container")
        else:
            pools = {"llm_corpus": container.corpus_tx, "llm_generation": container.generation_tx}

            async def one(tx: Any) -> str | None:
                """``None`` when writable, else the exception type that stopped the probe."""
                try:
                    await asyncio.wait_for(
                        PgStoreProbe(tx, READINESS_DB_TIMEOUT_SECONDS).require_writable(),
                        READINESS_DB_TIMEOUT_SECONDS + 0.1,
                    )
                except Exception as exc:  # noqa: BLE001 - not proving a writable primary: not ready
                    return type(exc.__cause__ or exc).__name__
                return None

            results = await asyncio.gather(*(one(tx) for tx in pools.values()))
            causes.update({n: r for n, r in zip(pools, results, strict=True) if r})
            failed.extend(causes)
        ok = not failed
        if ok != last[0] and not ok:
            log.warning("not_ready", failed=failed, causes=causes)
        elif ok != last[0] and last[0] is not None:
            log.info("ready_again")
        last[0] = ok
        return ok

    return check


def install_probes(app: FastAPI, settings: Settings) -> None:
    """Shadow the platform's check-less ``/ready`` with one that gates on the databases and relays.

    ``create_platform_app`` mounts a health router with no readiness checks and offers no hook for
    them. The same router, built with the check, is included and moved to the front of the route
    list, so it answers ``/health``, ``/live`` and ``/ready`` first (the first match wins).
    ``/live`` and ``/health`` behave as before: a stalled relay takes the pod out of rotation, not
    out of service (the relay recovers by itself once the database does).
    """
    checks: list[ReadinessCheck] = [_databases_ready(app)]
    if settings.events_enabled:
        checks.insert(0, lambda: _relay_ready(app))
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
