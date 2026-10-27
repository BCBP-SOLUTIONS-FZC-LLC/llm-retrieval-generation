"""A-5, A-6 and A-7: regeneration and fact-conflict resolution (§5.7.9, §5.7.10).

``Idempotency-Key`` is accepted and ignored on A-5 and A-6 (``base_version_id`` deduplicates).
A-6 streams exactly as R-3 does (§5.6): every failure before the first byte is an ordinary
problem, only ``INFERENCE_UNAVAILABLE`` and ``INTERNAL_ERROR`` ride the terminal ``error`` frame.
"""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Header, Path, Request, Response
from fastapicommon.context import get_request_context_or_none

from rag.application.generation.regenerate import (
    RegenerateDocumentCommand,
    RegenerateSectionCommand,
)
from rag.domain.shared.enums import ConflictResolution
from rag.domain.shared.identity import RequestIdentity
from rag.presentation.api.public.generation import SSE_HEADERS, _probe, _sse
from rag.presentation.api.public.sse import SseResponse
from rag.presentation.api.public.workflows_models import (
    FactConflictResolutionRequest,
    FactConflictResolutionResponse,
    RegenerateDocumentRequest,
    RegenerateSectionRequest,
    RunHandleResponse,
    recipients_of,
)
from rag.presentation.identity import require_identity
from rag.presentation.workflows_dependencies import WorkflowServices, get_workflows

router = APIRouter(prefix="/api/v1/artefacts", tags=["regeneration"])

Identity = Annotated[RequestIdentity, Depends(require_identity)]
Services = Annotated[WorkflowServices, Depends(get_workflows)]
IgnoredIdempotencyKey = Annotated[
    str | None, Header(alias="Idempotency-Key", min_length=1, max_length=255)
]
SectionRef = Annotated[str, Path(min_length=1, max_length=256)]
__all__ = ["SSE_HEADERS", "router"]


def _correlation() -> str:
    context = get_request_context_or_none()
    return (context.correlation_id or "") if context else ""


@router.post("/{artefact_id}/regenerate", response_model=RunHandleResponse, status_code=202)
async def regenerate_document(
    artefact_id: UUID,
    body: RegenerateDocumentRequest,
    response: Response,
    identity: Identity,
    services: Services,
    idempotency_key: IgnoredIdempotencyKey = None,
) -> RunHandleResponse:
    """A-5: document scope. ``202`` and a handle resolved through W-2.

    A repeat from the same base version answers with the existing handle (``200``).
    """
    handle = await services.regenerate_document.execute(
        identity,
        RegenerateDocumentCommand(
            artefact_id=artefact_id,
            base_version_id=body.base_version_id,
            instruction=body.instruction,
            recipients=recipients_of(body.recipients),
            correlation_id=_correlation(),
        ),
    )
    if handle.replayed:
        response.status_code = 200
    return RunHandleResponse.of(handle)


@router.post("/{artefact_id}/sections/{section_ref}/regenerate")
async def regenerate_section(
    artefact_id: UUID,
    section_ref: SectionRef,
    body: RegenerateSectionRequest,
    request: Request,
    identity: Identity,
    services: Services,
    idempotency_key: IgnoredIdempotencyKey = None,
) -> SseResponse:
    """A-6: section scope, streamed like R-3; the verdict frame names the new and prior versions."""
    stream = await services.regenerate_section.execute(
        identity,
        RegenerateSectionCommand(
            artefact_id=artefact_id,
            section_ref=section_ref,
            base_version_id=body.base_version_id,
            instruction=body.instruction,
            review_context=body.review_context,
            recipients=recipients_of(body.recipients),
            correlation_id=_correlation(),
        ),
        is_disconnected=_probe(request),
    )
    return _sse(stream, request, identity)


@router.post(
    "/{artefact_id}/sections/{section_ref}/fact-conflict-resolution",
    response_model=FactConflictResolutionResponse,
)
async def resolve_fact_conflict(
    artefact_id: UUID,
    section_ref: SectionRef,
    body: FactConflictResolutionRequest,
    identity: Identity,
    services: Services,
) -> FactConflictResolutionResponse:
    """A-7: resolve a surfaced conflict once; the regeneration proceeds as a new request."""
    result = await services.resolve_conflict.execute(
        identity,
        artefact_id,
        section_ref,
        body.conflict_id,
        ConflictResolution(body.resolution),
    )
    return FactConflictResolutionResponse(
        conflict_id=result.conflict_id,
        resolution=result.resolution.value,
        proceeds=result.proceeds,
        flagged_section_ids=list(result.flagged_section_ids),
    )
