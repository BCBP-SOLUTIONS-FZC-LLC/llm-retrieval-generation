"""R-2, R-3 and R-4: grounded answer, section draft (SSE) and structured extraction (JSON).

Authentication is declared per route (API-11). The status of a streaming response commits at
the first byte (API-6): every failure before it is an ordinary problem raised by the use case's
``execute``, and only a failure after it is the terminal ``error`` frame. The handler copies
nothing request-scoped into the body (LD-23): the producer binds the tenant itself.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Annotated

from fastapi import APIRouter, Depends, Header, Request, Response
from fastapicommon.context import get_request_context_or_none

from rag.application.generation.generate import GenerationStream
from rag.application.generation.requests import AnswerCommand, DraftCommand, ExtractCommand
from rag.domain.shared.identity import RequestIdentity
from rag.presentation.api.public.generation_models import (
    AnswerRequest,
    DraftRequest,
    ExtractionResponse,
    ExtractRequest,
)
from rag.presentation.api.public.sse import (
    ErrorContext,
    SseResponse,
    encode,
    supports_disconnect_probe,
)
from rag.presentation.generate_dependencies import GenerateServices, get_generate
from rag.presentation.identity import require_identity

router = APIRouter(prefix="/api/v1", tags=["generation"])

Identity = Annotated[RequestIdentity, Depends(require_identity)]
Services = Annotated[GenerateServices, Depends(get_generate)]
IdempotencyKey = Annotated[
    str | None, Header(alias="Idempotency-Key", min_length=1, max_length=255)
]
SSE_HEADERS = {"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}


def _correlation() -> str:
    context = get_request_context_or_none()
    return (context.correlation_id or "") if context else ""


def _sse(stream: GenerationStream, request: Request, identity: RequestIdentity) -> SseResponse:
    """The ``text/event-stream`` response over a stream the use case already holds open."""
    context = get_request_context_or_none()
    ctx = ErrorContext(
        base=request.app.state.settings.problem_type_base,
        instance=request.url.path,
        trace_id=(context.trace_id or "") if context else "",
        tenant_id=str(identity.tenant_id),
    )

    async def body() -> AsyncIterator[bytes]:
        events = stream.events()
        try:
            async for frame in events:
                yield encode(frame, ctx).encode()
        finally:
            await events.aclose()  # a dropped client reaches the stream now, not at GC (LD-18)

    return SseResponse(body(), media_type="text/event-stream", headers=SSE_HEADERS)


def _probe(request: Request) -> Callable[[], Awaitable[bool]] | None:
    return request.is_disconnected if supports_disconnect_probe(request.scope) else None


@router.post("/answers")
async def answers(
    body: AnswerRequest,
    request: Request,
    identity: Identity,
    services: Services,
    idempotency_key: IdempotencyKey = None,
) -> SseResponse:
    """R-2: a grounded answer streamed as ``draft`` frames then one ``verdict`` (§5.7.2)."""
    stream = await services.answer.execute(
        identity,
        AnswerCommand(
            question=body.question,
            scope=body.scope.to_spec(),
            requested_mode=body.mode(),
            idempotency_key=idempotency_key,
            correlation_id=_correlation(),
        ),
        is_disconnected=_probe(request),
    )
    return _sse(stream, request, identity)


@router.post("/sections/drafts")
async def section_drafts(
    body: DraftRequest,
    request: Request,
    identity: Identity,
    services: Services,
    idempotency_key: IdempotencyKey = None,
) -> SseResponse:
    """R-3: an interactive section draft, same transport as R-2 (§5.7.3)."""
    stream = await services.draft.execute(
        identity,
        DraftCommand(
            tender_id=body.tender_id,
            tender_section_ref=body.tender_section_ref,
            section_type=body.section_type,
            scope=body.scope.to_spec(body.tender_id),
            user_prompt=body.user_prompt,
            requested_mode=body.mode(),
            idempotency_key=idempotency_key,
            correlation_id=_correlation(),
        ),
        is_disconnected=_probe(request),
    )
    return _sse(stream, request, identity)


@router.post("/extractions", response_model=ExtractionResponse)
async def extractions(
    body: ExtractRequest,
    response: Response,
    identity: Identity,
    services: Services,
    idempotency_key: IdempotencyKey = None,
) -> ExtractionResponse:
    """R-4: structured extraction, synchronous, never streamed. ``206`` where degraded."""
    outcome = await services.extract.execute(
        identity,
        ExtractCommand(
            tender_id=body.tender_id,
            schema_ref=body.schema_ref,
            scope=body.scope.to_spec(body.tender_id),
            document_ids=tuple(body.document_ids),
            trigger_document_version_id=body.trigger_document_version_id,
            idempotency_key=idempotency_key,
            correlation_id=_correlation(),
        ),
    )
    if outcome.degraded:
        response.status_code = 206
    return ExtractionResponse.of(outcome)
