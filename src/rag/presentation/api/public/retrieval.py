"""R-1 search, R-6 classification proposal, R-7 required-input checks (§5.7.1, 5.7.6, 5.7.7)."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Response

from rag.application.retrieval.dtos import (
    ClassificationCommand,
    PipelineRequest,
    RequiredInputCommand,
    SearchCommand,
)
from rag.domain.shared.enums import GrantLevel
from rag.domain.shared.identity import RequestIdentity
from rag.presentation.api.public.retrieval_models import (
    ClassifyRequest,
    ClassifyResponse,
    RequiredInputRequest,
    RequiredInputResponse,
    SearchRequest,
    SearchResponse,
)
from rag.presentation.identity import require_identity
from rag.presentation.retrieval_dependencies import RetrievalServices, get_retrieval

router = APIRouter(prefix="/api/v1", tags=["retrieval"])

Identity = Annotated[RequestIdentity, Depends(require_identity)]
Services = Annotated[RetrievalServices, Depends(get_retrieval)]


@router.post("/search", response_model=SearchResponse)
async def search(
    body: SearchRequest, response: Response, identity: Identity, services: Services
) -> SearchResponse:
    """R-1: hybrid retrieval, results only. ``206`` where any stage degraded (§5.7.1)."""
    result = await services.search.execute(
        identity,
        SearchCommand(
            PipelineRequest(
                query=body.query,
                scope=body.scope.to_spec(),
                section_type=body.section_type,
                limit=body.limit,
                required_level=GrantLevel.SEARCH,
            )
        ),
    )
    if result.outcome.degraded:
        response.status_code = 206
    return SearchResponse.of(result)


@router.post("/classification/propose", response_model=ClassifyResponse)
async def propose_classification(
    body: ClassifyRequest, identity: Identity, services: Services
) -> ClassifyResponse:
    """R-6: stateless role proposal. No collection scope is resolved (§5.7.6)."""
    result = await services.classify.execute(
        identity, ClassificationCommand(body.content, body.filename, body.domain_profile_ref)
    )
    return ClassifyResponse.of(result)


@router.post("/required-input-checks", response_model=RequiredInputResponse)
async def required_input_checks(
    body: RequiredInputRequest, identity: Identity, services: Services
) -> RequiredInputResponse:
    """R-7: the pack's required-input rules against the resolved scope; ``200`` always."""
    result = await services.required_input.execute(
        identity,
        RequiredInputCommand(
            body.tender_id,
            body.scope.to_spec(body.tender_id),
            body.capability,
            tuple(dict.fromkeys(body.section_types)),
        ),
    )
    return RequiredInputResponse.of(result)
