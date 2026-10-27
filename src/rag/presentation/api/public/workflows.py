"""W-1 to W-7: long-form generation, extraction start, state, gate, cancel and resume.

Authentication is declared per route (API-11). ``Idempotency-Key`` is required on W-1 and W-6
(RUN-3). Gate and resume routes re-resolve scope and eligibility before they act (ASYNC-4);
nothing here ever queries the workflow engine (ASYNC-5).
"""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Header, Response
from fastapicommon.context import get_request_context_or_none

from rag.application.workflows.signals import OutlineEdit, OutlineEntry
from rag.application.workflows.start import StartExtractionCommand, StartGenerationCommand
from rag.domain.shared.identity import RequestIdentity
from rag.presentation.api.public.workflows_models import (
    ApproveRequest,
    CancelRequest,
    GenerationStateResponse,
    ResumeRequest,
    ReviseRequest,
    RunHandleResponse,
    SignalResponse,
    StartExtractionRequest,
    StartGenerationRequest,
    recipients_of,
)
from rag.presentation.identity import require_identity
from rag.presentation.workflows_dependencies import WorkflowServices, get_workflows

router = APIRouter(prefix="/api/v1", tags=["workflows"])

Identity = Annotated[RequestIdentity, Depends(require_identity)]
Services = Annotated[WorkflowServices, Depends(get_workflows)]
RequiredIdempotencyKey = Annotated[
    str, Header(alias="Idempotency-Key", min_length=1, max_length=255)
]


def _correlation() -> str:
    context = get_request_context_or_none()
    return (context.correlation_id or "") if context else ""


@router.post("/generations", response_model=RunHandleResponse, status_code=202)
async def start_generation(
    body: StartGenerationRequest,
    response: Response,
    identity: Identity,
    services: Services,
    idempotency_key: RequiredIdempotencyKey,
) -> RunHandleResponse:
    """W-1: ``202`` with the handle; ``200`` where the key replays the same body (§5.7.12)."""
    handle = await services.start.execute(
        identity,
        StartGenerationCommand(
            tender_id=body.tender_id,
            capability=body.capability,
            idempotency_key=idempotency_key,
            target_ref=body.target_ref,
            scope=body.scope.to_spec(body.tender_id),
            requested_mode=body.mode(),
            outline_gate=body.outline_gate,
            recipients=recipients_of(body.recipients),
            correlation_id=_correlation(),
        ),
    )
    if handle.replayed:
        response.status_code = 200
    return RunHandleResponse.of(handle)


@router.post("/extractions/async", response_model=RunHandleResponse, status_code=202)
async def start_async_extraction(
    body: StartExtractionRequest,
    response: Response,
    identity: Identity,
    services: Services,
    idempotency_key: RequiredIdempotencyKey,
) -> RunHandleResponse:
    """W-6: an extraction beyond R-4's synchronous budget, as a workflow."""
    handle = await services.start_extraction.execute(
        identity,
        StartExtractionCommand(
            tender_id=body.tender_id,
            schema_ref=body.schema_ref,
            idempotency_key=idempotency_key,
            target_ref=body.target_ref,
            scope=body.scope.to_spec(body.tender_id),
            trigger_document_version_id=body.trigger_document_version_id,
            recipients=recipients_of(body.recipients),
            correlation_id=_correlation(),
        ),
    )
    if handle.replayed:
        response.status_code = 200
    return RunHandleResponse.of(handle)


@router.get("/generations/{generation_id}", response_model=GenerationStateResponse)
async def get_generation(
    generation_id: UUID, identity: Identity, services: Services
) -> GenerationStateResponse:
    """W-2: state, outline, per-section progress, facts, coherence (never from Temporal)."""
    return GenerationStateResponse.of(await services.state.execute(identity, generation_id))


@router.post(
    "/generations/{generation_id}/outline/approve", response_model=SignalResponse, status_code=202
)
async def approve_outline(
    generation_id: UUID, body: ApproveRequest, identity: Identity, services: Services
) -> SignalResponse:
    """W-3: approve the outline; stage 1 begins when the signal lands."""
    return SignalResponse.of(
        await services.approve.execute(identity, generation_id, body.outline_version)
    )


@router.post("/generations/{generation_id}/outline", response_model=SignalResponse, status_code=202)
async def revise_outline(
    generation_id: UUID, body: ReviseRequest, identity: Identity, services: Services
) -> SignalResponse:
    """W-4: submit an edited outline; the workflow loops back to outline construction."""
    edit = OutlineEdit(
        body.outline_version, tuple(OutlineEntry(s.ref, s.type) for s in body.sections)
    )
    return SignalResponse.of(await services.revise.execute(identity, generation_id, edit))


@router.post("/generations/{generation_id}/cancel", response_model=SignalResponse, status_code=202)
async def cancel_generation(
    generation_id: UUID,
    identity: Identity,
    services: Services,
    body: CancelRequest | None = None,
) -> SignalResponse:
    """W-5: cancel; completed sections remain durable and readable."""
    reason = None if body is None else body.reason
    return SignalResponse.of(await services.cancel.execute(identity, generation_id, reason))


@router.post("/generations/{generation_id}/resume", response_model=SignalResponse, status_code=202)
async def resume_generation(
    generation_id: UUID,
    identity: Identity,
    services: Services,
    body: ResumeRequest | None = None,
) -> SignalResponse:
    """W-7: resume a run suspended on quota."""
    return SignalResponse.of(await services.resume.execute(identity, generation_id))
