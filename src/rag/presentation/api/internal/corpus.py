"""C-I1, C-I2, C-I4, C-I5, C-I6 and C-I8: the internal Ingestion routes (§5.7.16).

Mesh mTLS authenticates the caller; each route declares ``require_tenant`` (LD-95), which
answers 401 where ``x-tenant-id`` is absent, repeated or not a uuid. The tenant is the header's,
never the body's (API-1); RLS binds the same value.
"""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, Response

from rag.application.corpus.commands import (
    ActivateGenerationCommand,
    MigrationOutcome,
    MigrationResultCommand,
    ResolveCollectionCommand,
)
from rag.presentation.api.corpus_schemas import (
    ActivateRequest,
    ActivateResponse,
    MigrationResultRequest,
    MigrationResultResponse,
    PlacementResponse,
    PurgeEpochResponse,
    ResolveRequest,
    RetentionResponse,
)
from rag.presentation.corpus_dependencies import CorpusServices, get_corpus_services
from rag.presentation.identity import require_tenant

router = APIRouter(prefix="/api/v1/internal/collections", tags=["internal"])


@router.post("/resolve", response_model=PlacementResponse)
async def resolve_collection(
    body: ResolveRequest,
    response: Response,
    tenant_id: UUID = Depends(require_tenant),
    services: CorpusServices = Depends(get_corpus_services),
) -> PlacementResponse:
    """C-I1: resolve or create. 201 where this call created the row, 200 otherwise."""
    result = await services.resolve.execute(
        ResolveCollectionCommand(tenant_id, body.tender_id, body.tender_restricted)
    )
    response.status_code = 201 if result.created else 200
    return PlacementResponse.of(result.placement)


@router.get("/{collection_id}", response_model=PlacementResponse)
async def get_placement(
    collection_id: UUID,
    _tenant: UUID = Depends(require_tenant),
    services: CorpusServices = Depends(get_corpus_services),
) -> PlacementResponse:
    """C-I2: placement and generation metadata, in every status (COL-13)."""
    return PlacementResponse.of(await services.placement.execute(collection_id))


@router.post(
    "/{collection_id}/index-generations/{generation}/activate", response_model=ActivateResponse
)
async def activate_generation(
    collection_id: UUID,
    generation: int,
    body: ActivateRequest,
    tenant_id: UUID = Depends(require_tenant),
    services: CorpusServices = Depends(get_corpus_services),
) -> ActivateResponse:
    """C-I4: atomic cutover of a verified generation; idempotent for an active one (LD-37)."""
    return ActivateResponse.of(
        await services.activate.execute(
            ActivateGenerationCommand(tenant_id, collection_id, generation, body.record_version)
        )
    )


@router.get("/{collection_id}/retention", response_model=RetentionResponse)
async def get_retention(
    collection_id: UUID,
    _tenant: UUID = Depends(require_tenant),
    services: CorpusServices = Depends(get_corpus_services),
) -> RetentionResponse:
    """C-I5: the policy the retention sweep applies (no v1 caller)."""
    return RetentionResponse.of(await services.get_retention.execute(collection_id))


@router.post("/{collection_id}/purge-epoch", response_model=PurgeEpochResponse)
async def bump_purge_epoch(
    collection_id: UUID,
    _tenant: UUID = Depends(require_tenant),
    services: CorpusServices = Depends(get_corpus_services),
) -> PurgeEpochResponse:
    """C-I6: atomic increment; takes no ``record_version`` (LD-98)."""
    return PurgeEpochResponse.of(await services.bump_purge_epoch.execute(collection_id))


@router.post("/{collection_id}/migration-result", response_model=MigrationResultResponse)
async def migration_result(
    collection_id: UUID,
    body: MigrationResultRequest,
    tenant_id: UUID = Depends(require_tenant),
    services: CorpusServices = Depends(get_corpus_services),
) -> MigrationResultResponse:
    """C-I8: the promotion copy result; ``complete`` triggers the cutover."""
    return MigrationResultResponse.of(
        await services.migration_result.execute(
            MigrationResultCommand(
                tenant_id,
                collection_id,
                body.migration_id,
                MigrationOutcome(body.result),
                body.points_copied,
                body.reason,
            )
        )
    )
