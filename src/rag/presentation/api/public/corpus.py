"""P-1, P-2, P-3 and P-6: corpus administration (§5.7.15)."""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, Query, Response
from fastapicommon.rbac import RequireAnyRole, RequireRole

from rag.application.corpus.commands import (
    CollectionQuery,
    ListCollectionsQuery,
    ListGrantsQuery,
    PutRetentionCommand,
)
from rag.domain.shared.identity import RequestIdentity
from rag.infrastructure.config import Settings
from rag.presentation.api.corpus_schemas import (
    CollectionListResponse,
    CollectionResponse,
    GrantListResponse,
    PutRetentionRequest,
    PutRetentionResponse,
    vars_of,
)
from rag.presentation.corpus_dependencies import CorpusServices, get_corpus_services
from rag.presentation.dependencies import get_settings
from rag.presentation.identity import require_identity

router = APIRouter(prefix="/api/v1/collections", tags=["collections"])


def page(limit: int | None, offset: int, settings: Settings) -> tuple[int, int]:
    """``(limit, offset)``: default, and a larger limit is clamped, not rejected (§5.1)."""
    return min(limit or settings.page_limit_default, settings.page_limit_max), offset


@router.get("", response_model=CollectionListResponse)
async def list_collections(
    limit: int | None = Query(default=None, ge=1),
    offset: int = Query(default=0, ge=0),
    identity: RequestIdentity = Depends(require_identity),
    services: CorpusServices = Depends(get_corpus_services),
    settings: Settings = Depends(get_settings),
) -> CollectionListResponse:
    """P-1: collections in the caller's resolved scope; an empty scope is an empty list."""
    lim, off = page(limit, offset, settings)
    return CollectionListResponse.of(
        await services.list_collections.execute(ListCollectionsQuery(identity, lim, off))
    )


@router.get("/{collection_id}", response_model=CollectionResponse)
async def get_collection(
    collection_id: UUID,
    identity: RequestIdentity = Depends(require_identity),
    services: CorpusServices = Depends(get_corpus_services),
) -> CollectionResponse:
    """P-2: one collection; absent and ungranted are both 404 (CG-8)."""
    return CollectionResponse.of(
        await services.get_collection.execute(CollectionQuery(identity, collection_id))
    )


@router.get("/{collection_id}/grants", response_model=GrantListResponse)
async def list_grants(
    collection_id: UUID,
    limit: int | None = Query(default=None, ge=1),
    offset: int = Query(default=0, ge=0),
    identity: RequestIdentity = Depends(require_identity),
    _role: None = Depends(RequireAnyRole("tenant_admin", "tender_admin")),
    services: CorpusServices = Depends(get_corpus_services),
    settings: Settings = Depends(get_settings),
) -> GrantListResponse:
    """P-3: the last recorded overlay answer, for diagnosis. Never authoritative (CG-2)."""
    lim, off = page(limit, offset, settings)
    return GrantListResponse.of(
        await services.list_grants.execute(ListGrantsQuery(identity, collection_id, lim, off))
    )


@router.put("/{collection_id}/retention", response_model=PutRetentionResponse)
async def put_retention(
    collection_id: UUID,
    body: PutRetentionRequest,
    response: Response,
    identity: RequestIdentity = Depends(require_identity),
    _role: None = Depends(RequireRole("tenant_admin")),
    services: CorpusServices = Depends(get_corpus_services),
) -> PutRetentionResponse:
    """P-6: no ``record_version`` creates (201); one updates (200); anything else is 409."""
    result = await services.put_retention.execute(
        PutRetentionCommand(
            identity, collection_id, body.retain_days, body.legal_hold, body.record_version
        )
    )
    response.status_code = 201 if result.created else 200
    return PutRetentionResponse(**vars_of(result.policy))
