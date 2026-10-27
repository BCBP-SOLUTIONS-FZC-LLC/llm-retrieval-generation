"""C-M1 ``GET /api/internal/models``: the Model Gateway's catalogue (§5.7.18).

Reachable by mutual TLS and NetworkPolicy alone: the caller (User Profile) holds no tenant, so the
route declares no auth dependency and answers no 401 or 400. ``API-11`` and ``CI-1`` exempt it
by name (``presentation.api.internal.models.EXEMPT_PATH``).
"""

from __future__ import annotations

from fastapi import APIRouter, Depends

from rag.presentation.api.corpus_schemas import ModelCatalogueResponse
from rag.presentation.corpus_dependencies import CorpusServices, get_corpus_services

EXEMPT_PATH = "/api/internal/models"

router = APIRouter(tags=["internal"])


@router.get(EXEMPT_PATH, response_model=ModelCatalogueResponse)
async def list_models(
    services: CorpusServices = Depends(get_corpus_services),
) -> ModelCatalogueResponse:
    """C-M1: model ids a tenant may select; 503 INFERENCE_UNAVAILABLE where unreadable."""
    return ModelCatalogueResponse.of(await services.models.execute())
