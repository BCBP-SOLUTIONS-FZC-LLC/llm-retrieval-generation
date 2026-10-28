"""Write use cases: audit append, version supersession, ART-9 purge, EVT-1 purge markers."""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from fastapicommon.logging import get_logger

from rag.application.generation.ports import ArtefactContentStore
from rag.application.shared.ports import TransactionRunner, TxHandle
from rag.domain.generation import audit
from rag.domain.generation.audit import AuditEntry, AuditEntryDraft
from rag.domain.generation.repositories import (
    ArtefactPurgeRepository,
    AuditChainRepository,
    Clock,
    RetrievalSetRepository,
    VersionRepository,
)
from rag.domain.shared.enums import AuditActorType

log = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class AppendAuditEntry:
    """Append one chain entry in the caller's transaction (AC-1..AC-9).

    The database allocates ``seq`` under the per-tenant advisory lock and computes the hashes.
    A repeat of the same ``source_event_id`` appends nothing and returns ``None`` (AC-9), so a
    retried writing transaction is safe.
    """

    repo: AuditChainRepository

    async def __call__(self, tx: TxHandle, draft: AuditEntryDraft) -> AuditEntry | None:
        """Append ``draft``."""
        return await self.repo.append(tx, draft)


@dataclass(frozen=True, slots=True)
class MarkVersionsSuperseded:
    """AV-6: flag every document version whose section set a section regeneration changes."""

    repo: VersionRepository

    async def __call__(self, tx: TxHandle, artefact_id: UUID, section_id: UUID) -> list[UUID]:
        """Ids newly flagged: ``superseded_version_ids`` on ``SectionCompleted``."""
        return await self.repo.mark_superseded_by_section_edit(tx, artefact_id, section_id)


@dataclass(frozen=True, slots=True)
class PurgeResult:
    """Outcome of a purge: the artefacts removed and the S3 clean-up that did not complete."""

    artefact_ids: tuple[UUID, ...]
    s3_deleted: int
    s3_failed: bool


@dataclass(frozen=True, slots=True)
class PurgeArtefacts:
    """ART-9: ``TenderPurged`` (one tender) or a tenant purge (``tender_id=None``, EVT-15).

    One transaction per call: one ``llm.artefact.purged`` chain entry per artefact, then the
    delete with constraints deferred. The chain deletes nothing (AC-6). S3 objects are deleted
    after the commit; a failure there is reported, not raised, because the orphan sweep (21.2)
    removes what it leaves and the database erasure has already happened.
    """

    tx: TransactionRunner
    purge: ArtefactPurgeRepository
    audit: AppendAuditEntry
    store: ArtefactContentStore
    clock: Clock

    async def execute(
        self,
        *,
        tenant_id: UUID,
        tender_id: UUID | None,
        event_id: UUID,
        service_account_id: UUID,
        reason: str,
    ) -> PurgeResult:
        """Delete the scope's artefacts; ``event_id`` seeds each entry's idempotent source id."""

        async def work(tx: TxHandle) -> list[UUID]:
            artefacts = await self.purge.list_artefacts_for_purge(tx, tenant_id, tender_id)
            now = self.clock.now()
            for a in artefacts:
                await self.audit(
                    tx,
                    AuditEntryDraft(
                        tenant_id=tenant_id,
                        occurred_at=now,
                        entry_type=audit.ARTEFACT_PURGED[0],
                        action=audit.ARTEFACT_PURGED[1],
                        actor_type=AuditActorType.SERVICE_ACCOUNT,
                        actor_id=service_account_id,
                        artefact_id=a.id,
                        target_type="artefact",
                        target_id=str(a.id),
                        source_event_type=audit.ARTEFACT_PURGED[0],
                        source_event_id=f"{event_id}:{a.id}",
                        metadata={"reason": reason},
                    ),
                )
            ids = [a.id for a in artefacts]
            await self.purge.delete_artefact_trees(tx, tenant_id, tender_id, ids)
            return ids

        ids = await self.tx.run_in_tx(work)
        deleted, failed = 0, False
        try:
            if tender_id is None:
                deleted = await self.store.delete_prefix(tenant_id)
            else:
                for artefact_id in ids:
                    deleted += await self.store.delete_prefix(tenant_id, artefact_id)
        except Exception:  # noqa: BLE001 - the sweep reclaims; the DB erasure already committed
            log.exception("s3_delete_after_purge_failed")  # the orphan sweep removes the objects
            failed = True
        return PurgeResult(tuple(ids), deleted, failed)


@dataclass(frozen=True, slots=True)
class RecordDocumentPurge:
    """EVT-1: add purge markers beside every retrieval set that cites a purged document.

    Matches ``(tenant_id, collection_id, document_id, document_version)`` against the chunks
    the sets recorded, never against the corpus. The set, its tier and its report are not
    touched (EVT-2); ``uq_purge_event`` makes a redelivery a no-op (RSP-1). The ``processed_events``
    ledger row and the epoch bump belong to the consumer (PE-2).
    """

    tx: TransactionRunner
    repo: RetrievalSetRepository

    async def execute(
        self,
        *,
        tenant_id: UUID,
        collection_id: UUID,
        document_id: UUID,
        document_version: int,
        event_id: UUID,
    ) -> int:
        """Number of new marker rows."""
        return await self.tx.run_in_tx(
            lambda tx: self.repo.purge_for_document(
                tx,
                tenant_id=tenant_id,
                collection_id=collection_id,
                document_id=document_id,
                document_version=document_version,
                event_id=event_id,
            )
        )
