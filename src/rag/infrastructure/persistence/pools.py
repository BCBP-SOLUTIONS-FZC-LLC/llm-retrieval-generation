"""Pool factories for the two Postgres instances (§4.1), via ``pgcommon.Pool``."""

from __future__ import annotations

import os
from collections.abc import Iterator
from contextlib import AbstractContextManager, contextmanager
from dataclasses import replace
from uuid import UUID

from fastapicommon.context import get_request_context_or_none
from fastapicommon.logging import get_logger
from pgcommon import (
    Config,
    ConfigWarning,
    GUCSet,
    Pool,
    StructlogLogger,
    config_from_env,
    guc_set_from_context,
    with_guc_set,
)

from rag.infrastructure.config import Settings

log = get_logger(__name__)

# ``config_from_env`` also assembles a DSN from DATABASE_URL / PG_HOST / PG_USER ...; ours come
# from Settings (two databases, their own variable names), so warnings about those are noise.
_DSN_WARNING_KEYS = frozenset(
    {"DATABASE_URL", "PG_USER/PG_DBNAME", "PG_HOST", "PG_PORT", "PG_PASSWORD", "PG_SSLMODE"}
)
_warned: set[tuple[str, str]] = set()


def request_guc_provider() -> GUCSet | None:
    """GUC identity for the current request, from the platform ``RequestContext``.

    Outside a request (an event consumer, a sweep) it falls back to the scope bound by
    ``bind_tenant_scope`` / the eventcommon consumer, and is ``None`` when neither exists, so no
    GUC is bound and RLS (``NULLIF(..., '')``) matches nothing. An inconsistent set (user without
    tenant) is returned as is: ``pgcommon`` validates it on acquire and raises an
    ``IDENTITY_ERRORS`` member, which ``presentation.errors`` answers as 401.
    """
    context = get_request_context_or_none()
    if context is None or not (context.user_id or context.tenant_id):
        return guc_set_from_context()
    return GUCSet(
        user_id=context.user_id or "",
        tenant_id=context.tenant_id or "",
        tenant_roles=context.roles,
    )


def _log_warnings_once(warnings: list[ConfigWarning]) -> None:
    """The library asks for every warning to be logged at startup, once (a typo'd tuning value)."""
    for w in warnings:
        if w.key in _DSN_WARNING_KEYS or (w.key, w.reason) in _warned:
            continue
        _warned.add((w.key, w.reason))
        log.warning("pgcommon_config_warning", key=w.key, reason=w.reason)


def pool_config(
    dsn: str,
    name: str,
    settings: Settings,
    *,
    max_conns: int | None = None,
    min_conns: int | None = None,
    guc: bool = True,
    pgbouncer: bool = True,
) -> Config:
    """The ``pgcommon.Config`` of every pool this service opens; nothing else builds one.

    Tuning (``PG_STATEMENT_TIMEOUT``, ``PG_LOCK_TIMEOUT``, ``PG_ACQUIRE_TIMEOUT``,
    ``PG_SLOW_QUERY_THRESHOLD``, ``PG_MAX_CONN_LIFETIME`` and the rest of the library's ``PG_*``
    variables) is parsed and validated by ``pgcommon.config_from_env``, which replaces a bad value
    with a safe default and a warning, logged once. What stays ours, because it is a security or
    topology decision and not tuning: the DSN (two databases, their own variable names), the pool
    name and size (``PG_MAX_CONNS`` must equal PgBouncer's pool size, so a typo fails at startup
    instead of becoming a default), PgBouncer mode (read leniently by the library, asserted as
    exactly ``true`` by ``assert_isolation_config``) and the tenant GUC provider.
    """
    base, warnings = config_from_env()
    _log_warnings_once(warnings)
    return replace(
        base,
        dsn=dsn,
        pool_name=name,
        max_conns=max_conns if max_conns is not None else settings.pg_max_conns,
        min_conns=min_conns
        if min_conns is not None
        else (base.min_conns if "PG_MIN_CONNS" in os.environ else None),
        pgbouncer_mode=pgbouncer,
        guc_provider=request_guc_provider if guc else None,
        logger=StructlogLogger(),
    )


async def create_corpus_pool(settings: Settings) -> Pool:
    """Pool for ``llm_corpus`` (primary)."""
    return await Pool.create(pool_config(settings.corpus_database_url, "corpus", settings))


async def create_generation_pool(settings: Settings) -> Pool:
    """Pool for ``llm_generation``."""
    return await Pool.create(pool_config(settings.generation_database_url, "generation", settings))


@contextmanager
def bind_tenant_scope(tenant_id: UUID) -> Iterator[None]:
    """Bind ``tenant_id`` for every pool query in the block (RLS, §4.8.1), outside a request.

    Consumers bind the validated envelope tenant, sweeps the tenant they iterate. Nothing here
    trusts the caller: a ``UUID`` is required, so ``'system'`` (SYSTEM_TENANT_ID) cannot get in.
    """
    if not isinstance(tenant_id, UUID):
        raise TypeError(f"tenant_id must be a UUID, got {type(tenant_id).__name__}")
    with with_guc_set(GUCSet(tenant_id=str(tenant_id))):
        yield


class ContextTenantBinder:
    """``TenantBinder`` over ``bind_tenant_scope``: the stream producer's own tenant binding."""

    def bind(self, tenant_id: UUID) -> AbstractContextManager[None]:
        """Bind ``tenant_id`` for every pool query in the block (LD-23)."""
        return bind_tenant_scope(tenant_id)
