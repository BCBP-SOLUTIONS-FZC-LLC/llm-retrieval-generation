"""Wiring of the generation module's use cases (no DI framework: plain construction)."""

from __future__ import annotations

from dataclasses import dataclass

from fastapi import Request

from rag.application.generation.clock import SystemClock
from rag.application.generation.commands import (
    AppendAuditEntry,
    MarkVersionsSuperseded,
    PurgeArtefacts,
    RecordDocumentPurge,
)
from rag.application.generation.ports import ArtefactContentStore
from rag.application.generation.queries import (
    GetArtefact,
    GetRetrievalSet,
    GetSectionVersionContent,
    GetVersion,
    ListSections,
    ListVersions,
    RenderArtefact,
)
from rag.application.shared.ports import TransactionRunner
from rag.application.shared.scope import ScopeAuthorizer
from rag.infrastructure.config import Settings
from rag.infrastructure.persistence.generation.repositories import (
    PgArtefactPurgeRepository,
    PgArtefactReadRepository,
    PgAuditChainRepository,
    PgRetrievalSetRepository,
    PgVersionRepository,
)
from rag.infrastructure.storage.artefact_content_store import S3ArtefactContentStore
from rag.infrastructure.unwired_scope import UnwiredScopeAuthorizer


@dataclass(frozen=True, slots=True)
class GenerationServices:
    """Use cases of the generation record slice.

    Routes read these; the events slice reuses ``purge_artefacts``, ``record_document_purge``,
    ``append_audit`` and ``mark_superseded``.
    """

    get_artefact: GetArtefact
    list_versions: ListVersions
    get_version: GetVersion
    list_sections: ListSections
    get_section_content: GetSectionVersionContent
    render_artefact: RenderArtefact
    get_retrieval_set: GetRetrievalSet
    append_audit: AppendAuditEntry
    mark_superseded: MarkVersionsSuperseded
    purge_artefacts: PurgeArtefacts
    record_document_purge: RecordDocumentPurge


def build_generation_services(
    settings: Settings,
    tx: TransactionRunner,
    *,
    scope: ScopeAuthorizer | None = None,
    store: ArtefactContentStore | None = None,
) -> GenerationServices:
    """Construct the use cases over the generation pool's transaction runner."""
    scope = scope or UnwiredScopeAuthorizer()
    store = store or S3ArtefactContentStore(
        settings.s3_bucket,
        region=settings.s3_region,
        endpoint_url=settings.s3_endpoint_url,
        kms_key_id=settings.s3_kms_key_id,
    )
    reads, sets = PgArtefactReadRepository(), PgRetrievalSetRepository()
    audit = AppendAuditEntry(PgAuditChainRepository())
    return GenerationServices(
        get_artefact=GetArtefact(tx, reads, scope),
        list_versions=ListVersions(tx, reads, scope),
        get_version=GetVersion(tx, reads, scope),
        list_sections=ListSections(tx, reads, scope),
        get_section_content=GetSectionVersionContent(tx, reads, scope, store),
        render_artefact=RenderArtefact(tx, reads, scope),
        get_retrieval_set=GetRetrievalSet(tx, sets, scope),
        append_audit=audit,
        mark_superseded=MarkVersionsSuperseded(PgVersionRepository()),
        purge_artefacts=PurgeArtefacts(
            tx, PgArtefactPurgeRepository(), audit, store, SystemClock()
        ),
        record_document_purge=RecordDocumentPurge(tx, sets),
    )


def get_generation(request: Request) -> GenerationServices:
    """The app's generation services (override in tests with ``dependency_overrides``)."""
    services: GenerationServices = request.app.state.container.generation
    return services
