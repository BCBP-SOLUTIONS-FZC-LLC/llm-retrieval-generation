"""SQS consumers of §12.4: thin adapters from an envelope to one use case.

Every handler does the same four things, in this order, and nothing else:

1. **Tenant.** ``Envelope.tenant_id`` must be a UUID (``'system'`` is not, LD-19) and is bound
   with ``bind_tenant_scope`` before any query. A payload ``tenant_id`` that disagrees is a forgery.
2. **Contract.** The payload is parsed into the fields this service reads; a missing or malformed
   field is poison.
3. **Effect.** One use case. ``ForgedEvent`` and other permanent refusals are poison.
4. **Ledger.** ``processed_events`` under the consumer's own name (EVT-6). Where the effect lives
   in ``llm_generation`` the ledger row commits with it (``Store.process``, PE-1); where it lives in
   ``llm_corpus`` (a different instance) the use case is idempotent on its own and the ledger row
   follows it (``inbox.handler``) — the recorded PE-1 deviation.

*Poison* goes to the queue's DLQ through ``forward_source_message`` and the message is acked
(a decode error or schema violation goes straight to the DLQ, §12.4). An unknown event type is
acked and logged: the subscription filter is the queue's, and an addition on a shared topic must
not wedge the queue. Anything else raises, so SQS redelivers (the library dead-letters at the
configured receive count).
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from eventcommon import Envelope
from eventcommon.consumer import DLQPublisher, forward_source_message
from eventcommon.inbox import Store
from eventcommon.inbox import handler as ledger_handler

from rag.application.corpus.lifecycle import (
    ApplyTenantState,
    OffboardTenant,
    RestrictTender,
)
from rag.application.events import contracts as c
from rag.application.events.consumers import (
    CloseTenderCorpus,
    ForgedEvent,
    InvalidateProfileCache,
    ProjectDocumentPurge,
    PropagateTenderPurge,
    ResumeSuspendedRuns,
)
from rag.application.events.ports import LedgerTx
from rag.infrastructure.events.schemas import parse_consumed
from rag.infrastructure.persistence.pools import bind_tenant_scope

log = logging.getLogger(__name__)

Handler = Callable[[Envelope[Any]], Awaitable[None]]


class Poison(Exception):
    """A message that can never succeed: dead-lettered at once, never retried."""


@dataclass(frozen=True, slots=True)
class ConsumerServices:
    """The use cases the consumers call (built from ``Container.corpus`` / ``generation``)."""

    apply_tenant_state: ApplyTenantState
    offboard_tenant: OffboardTenant
    restrict_tender: RestrictTender
    close_tender: CloseTenderCorpus
    tender_purge: PropagateTenderPurge
    document_purge: ProjectDocumentPurge
    profile_invalidation: InvalidateProfileCache
    quota_resume: ResumeSuspendedRuns


def tenant_of(env: Envelope[Any], payload_tenant: UUID | None) -> UUID:
    """The envelope's tenant as a UUID, checked against the payload's (EVT-14)."""
    try:
        tenant = UUID(env.tenant_id)
    except ValueError as exc:
        raise Poison(f"tenant_id {env.tenant_id!r} is not a uuid") from exc
    if payload_tenant is not None and payload_tenant != tenant:
        raise Poison("payload tenant_id disagrees with the envelope's")
    return tenant


def _parse(env: Envelope[Any]) -> Any:
    try:
        return parse_consumed(env.type, env.data)
    except ValueError as exc:  # pydantic.ValidationError is one
        raise Poison(f"invalid {env.type} payload: {exc}") from exc


async def _seen(store: Store, env: Envelope[Any]) -> bool:
    """The read-only ledger lookup that precedes TX-1 (§13.4): a redelivery is a no-op (EVT-6)."""
    return await store.is_processed(UUID(env.id))


def _once(store: Store, env: Envelope[Any]) -> LedgerTx:
    async def once(fn: Callable[[Any], Awaitable[None]]) -> None:
        await store.process(env, fn)

    return once


# --- the six consumers ----------------------------------------------------------------------
# Each factory returns a handler that already carries its ledger (see the module docstring).


def purge_projection(svc: ConsumerServices, store: Store) -> Handler:
    """``llm-document-rag-q``: ``DocumentPurged`` (§13.4, EVT-1, EVT-14)."""

    async def handle(env: Envelope[Any]) -> None:
        if env.type != c.DOCUMENT_PURGED:
            return _ignore(env)
        p: c.DocumentPurged = _parse(env)
        tenant = tenant_of(env, p.tenant_id)
        with bind_tenant_scope(tenant):
            if await _seen(store, env):
                return
            await svc.document_purge.execute(
                tenant_id=tenant,
                collection_id=p.collection_id,
                document_id=p.document_id,
                document_version=p.document_version,
                event_id=UUID(env.id),
                once=_once(store, env),
            )

    return handle


def pack_invalidation(svc: ConsumerServices, store: Store) -> Handler:
    """``domainprofile-llm-invalidate-q``: ``DomainProfilePublished`` (EVT-4)."""

    async def handle(env: Envelope[Any]) -> None:
        if env.type != c.DOMAIN_PROFILE_PUBLISHED:
            return _ignore(env)
        p: c.DomainProfilePublished = _parse(env)
        tenant = tenant_of(env, p.tenant_id)
        with bind_tenant_scope(tenant):
            if await _seen(store, env):
                return
            await svc.profile_invalidation.execute(
                tenant_id=tenant,
                domain=p.domain,
                language=p.language,
                concern=p.concern,
                version=p.version,
                event_id=UUID(env.id),
                once=_once(store, env),
            )

    return handle


def tenant_offboarding(svc: ConsumerServices, store: Store) -> Handler:
    """``iam-rag-purge-q``: ``TenantMembershipsPurged``, phase one only (EVT-15).

    Nothing is deleted here: collections go to `purging` and the lifecycle row to `purged`; the
    scheduled ``tenant-purge`` sweep deletes after the soak. It is the *only* consumer that purges
    (EVT-5), and it alerts (an ERROR line the platform's log alert matches) on every first run.
    """

    async def handle(env: Envelope[Any]) -> None:
        if env.type != c.TENANT_MEMBERSHIPS_PURGED:
            return _ignore(env)
        p: c.TenantMembershipsPurged = _parse(env)
        tenant = tenant_of(env, p.tenant_id)
        with bind_tenant_scope(tenant):
            await svc.offboard_tenant.execute(tenant, "TenantMembershipsPurged")
        log.error("ALERT tenant %s offboarding phase one applied (event %s)", tenant, env.id)

    return ledger_handler(store, handle)


def tenant_state(svc: ConsumerServices, store: Store) -> Handler:
    """``iam-rag-membership-q``: ``TenantStateChanged`` (freeze/restore; ``offboarded`` ignored)."""

    async def handle(env: Envelope[Any]) -> None:
        if env.type != c.TENANT_STATE_CHANGED:
            return _ignore(env)
        p: c.TenantStateChanged = _parse(env)
        tenant = tenant_of(env, p.tenant_id)
        with bind_tenant_scope(tenant):
            await svc.apply_tenant_state.execute(tenant, p.status, p.cause)

    return ledger_handler(store, handle)


def tender_lifecycle(svc: ConsumerServices, store: Store) -> Handler:
    """``tender-rag-q``: restricted, purged and the four terminal outcomes (COL-8, ART-7, ART-9)."""

    async def handle(env: Envelope[Any]) -> None:
        if env.type not in c.CONSUMED or c.CONSUMED[env.type] is not c.TenderEvent:
            return _ignore(env)
        p: c.TenderEvent = _parse(env)
        tenant = tenant_of(env, p.tenant_id)
        with bind_tenant_scope(tenant):
            if env.type == c.TENDER_RESTRICTED:
                await svc.restrict_tender.execute(tenant, p.tender_id)
            elif env.type == c.TENDER_PURGED:
                await svc.tender_purge.execute(
                    tenant_id=tenant, tender_id=p.tender_id, event_id=UUID(env.id)
                )
            else:
                await svc.close_tender.execute(
                    tenant_id=tenant, tender_id=p.tender_id, event_id=UUID(env.id)
                )

    return ledger_handler(store, handle)


def quota_resume(svc: ConsumerServices, store: Store) -> Handler:
    """``usage-rag-q``: ``TenantQuotaRestored`` (LD-70, ASYNC-8)."""

    async def handle(env: Envelope[Any]) -> None:
        if env.type != c.TENANT_QUOTA_RESTORED:
            return _ignore(env)
        p: c.TenantQuotaRestored = _parse(env)
        tenant = tenant_of(env, p.tenant_id)
        with bind_tenant_scope(tenant):
            await svc.quota_resume.execute(resource=p.resource)

    return ledger_handler(store, handle)


def _ignore(env: Envelope[Any]) -> None:
    log.info("ignoring event type %s on this queue (id %s)", env.type, env.id)


def poison_safe(handler: Handler, dlq: DLQPublisher) -> Handler:
    """Dead-letter a message that can never succeed, and ack it (``forward_source_message``)."""

    async def guarded(env: Envelope[Any]) -> None:
        try:
            await handler(env)
        except (Poison, ForgedEvent) as exc:
            log.error("poison %s %s: %s", env.type, env.id, exc)
            await forward_source_message(dlq, f"poison: {exc}"[:256])

    return guarded
