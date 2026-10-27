"""R-5 ``GET /api/v1/retrieval-sets/{id}``: the explainability surface (LLD 5.7.5)."""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends

from rag.domain.shared.identity import RequestIdentity
from rag.presentation.api.public.artefacts_models import RetrievalSetResponse
from rag.presentation.generation_dependencies import GenerationServices, get_generation
from rag.presentation.identity import require_identity

router = APIRouter(prefix="/api/v1/retrieval-sets", tags=["retrieval"])


@router.get("/{retrieval_set_id}", response_model=RetrievalSetResponse)
async def get_retrieval_set(
    retrieval_set_id: UUID,
    identity: Annotated[RequestIdentity, Depends(require_identity)],
    services: Annotated[GenerationServices, Depends(get_generation)],
) -> RetrievalSetResponse:
    """R-5: the recorded set, its chunks (purged ones flagged, excerpts intact) and purge rows."""
    view = await services.get_retrieval_set.execute(identity, retrieval_set_id)
    return RetrievalSetResponse.model_validate(view)
