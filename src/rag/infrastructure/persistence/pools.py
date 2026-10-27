"""Pool factories for the two Postgres instances (§4.1), via ``pgcommon.Pool``."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import AbstractContextManager, contextmanager
from uuid import UUID

from fastapicommon.context import get_request_context_or_none
from pgcommon import Config, GUCSet, Pool, StructlogLogger, guc_set_from_context, with_guc_set

from rag.infrastructure.config import Settings


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


def pool_config(dsn: str, name: str, settings: Settings) -> Config:
    """``pgcommon.Config`` for one instance: PgBouncer mode on, request GUC provider set."""
    return Config(
        dsn=dsn,
        max_conns=settings.pg_max_conns,
        pgbouncer_mode=True,
        guc_provider=request_guc_provider,
        pool_name=name,
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
