"""W-8 ``POST /api/v1/operator/generations/{id}/rematerialise`` (§5.7.14, §18.9, API-14).

The operator prefix is the only one whose gateway route asserts ``platform_operator``; the use
case re-checks it. ``x-tenant-id`` names the tenant whose run is restarted: the tenant binding
(RLS) is the header's, so a run of another tenant is a ``404``.
"""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends

from rag.domain.shared.identity import RequestIdentity
from rag.presentation.api.public.workflows_models import (
    RematerialiseRequest,
    RematerialiseResponse,
)
from rag.presentation.identity import require_identity
from rag.presentation.workflows_dependencies import WorkflowServices, get_workflows

router = APIRouter(prefix="/api/v1/operator", tags=["operator"])

Identity = Annotated[RequestIdentity, Depends(require_identity)]
Services = Annotated[WorkflowServices, Depends(get_workflows)]


@router.post(
    "/generations/{generation_id}/rematerialise",
    response_model=RematerialiseResponse,
    status_code=202,
)
async def rematerialise(
    generation_id: UUID, body: RematerialiseRequest, identity: Identity, services: Services
) -> RematerialiseResponse:
    """W-8: start a lost run's workflow again from the run row; audited, ticket required."""
    result = await services.rematerialise.execute(
        identity, generation_id, body.ticket_ref, body.reason
    )
    return RematerialiseResponse.of(result)
