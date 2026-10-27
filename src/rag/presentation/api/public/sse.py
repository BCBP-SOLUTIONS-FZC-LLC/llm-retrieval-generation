"""Server-Sent Events framing of the two-phase stream (LLD §5.6, LD-1).

One event type per frame kind, no discriminator field. ``verdict`` is the terminal frame and
carries no generated text. ``error`` is an RFC 9457 problem detail (§24), the same shape an
HTTP failure before the first byte would have had.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from fastapi.responses import StreamingResponse
from starlette.types import Receive, Scope, Send

from rag.application.generation.frames import (
    DraftFrame,
    ErrorFrame,
    Frame,
    Heartbeat,
    VerdictFrame,
)
from rag.domain.generation.eligibility import EligibilityVerdict
from rag.domain.shared.errors import ERROR_TAXONOMY, PROBLEM_DOMAIN, ErrorCode

HEARTBEAT = ": heartbeat\n\n"
"""An SSE comment: keeps an idle proxy from reaping the connection (§5.6)."""


def eligibility_json(e: EligibilityVerdict) -> dict[str, Any]:
    """``EligibilityVerdict`` (§5.5, API-5)."""
    return {
        "state": e.state.value,
        "requested_mode": e.requested_mode.value,
        "effective_mode": e.effective_mode.value,
        "clamped": e.clamped,
        "clamp_reason": e.clamp_reason,
        "profile_version": e.profile_version,
    }


def verdict_json(f: VerdictFrame) -> dict[str, Any]:
    """The ``verdict`` event data (§5.6)."""
    body: dict[str, Any] = {
        "artefact_id": str(f.artefact_id),
        "generation_id": str(f.generation_id),
        "artefact_version_id": None
        if f.artefact_version_id is None
        else str(f.artefact_version_id),
        "version_no": f.version_no,
        "retrieval_set_id": None if f.retrieval_set_id is None else str(f.retrieval_set_id),
        "citations": [
            {
                "claim_ordinal": c.claim_ordinal,
                "outcome": c.outcome.value,
                "cited_chunk_ids": list(c.cited_chunk_ids),
                "corrected_chunk_ids": list(c.corrected_chunk_ids),
                "tier_decided": c.tier_decided,
            }
            for c in f.citations
        ],
        "groundedness": {
            "supported": f.groundedness.supported,
            "unsupported": f.groundedness.unsupported,
            "miscited": f.groundedness.miscited,
            "gaps": f.groundedness.gaps,
            "tiers_run": list(f.groundedness.tiers_run),
        },
        "completeness": {
            "verdict": f.completeness.verdict.value,
            "missed_items": list(f.completeness.missed_items),
        },
        "evidence_tier": f.evidence_tier.value,
        "eligibility": eligibility_json(f.eligibility),
        "degradation_markers": list(f.degradation_markers),
        "delivery": f.delivery.value,
    }
    if f.disclaimer_ref is not None:
        body["disclaimer_ref"] = f.disclaimer_ref
    if f.refusal is not None:
        body["refusal"] = f.refusal
    if f.replay:
        body["replay"] = True
    if f.section_version_id is not None:
        body["section_version_id"] = str(f.section_version_id)
        body["prior_section_version_id"] = (
            None if f.prior_section_version_id is None else str(f.prior_section_version_id)
        )
    if f.fact_conflict is not None:
        c = f.fact_conflict
        body["fact_conflict"] = {
            "conflict_id": str(c.conflict_id),
            "fact_key": c.fact_key,
            "established_value": c.established_value,
            "established_by_section_id": str(c.established_by_section_id),
            "instruction_value": c.instruction_value,
            "choices": list(c.choices),
        }
    return body


@dataclass(frozen=True, slots=True)
class ErrorContext:
    """What the terminal ``error`` frame's problem detail needs, captured before the stream."""

    base: str
    instance: str
    trace_id: str
    tenant_id: str


def error_json(f: ErrorFrame, ctx: ErrorContext) -> dict[str, Any]:
    """The ``error`` event data: the problem detail its code would have had before byte one."""
    if f.code == ErrorCode.INFERENCE_UNAVAILABLE.value:
        spec = ERROR_TAXONOMY[ErrorCode.INFERENCE_UNAVAILABLE]
        domain, slug, title, status = PROBLEM_DOMAIN, spec.slug, spec.title, spec.status
    else:
        domain, slug, title, status = "platform", "internal-error", "Internal error", 500
    return {
        "type": f"{ctx.base}/{domain}/{slug}",
        "title": title,
        "status": status,
        "detail": f.detail,
        "instance": ctx.instance,
        "traceId": ctx.trace_id,
        "errorCode": f.code,
        "tenantId": ctx.tenant_id,
        "timestamp": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }


def encode(frame: Frame, ctx: ErrorContext) -> str:
    """One frame as SSE text."""
    if isinstance(frame, Heartbeat):
        return HEARTBEAT
    if isinstance(frame, DraftFrame):
        data = {"delta": frame.delta, "citation_refs": list(frame.citation_refs)}
        return f"event: draft\ndata: {json.dumps(data)}\n\n"
    if isinstance(frame, VerdictFrame):
        return f"event: verdict\ndata: {json.dumps(verdict_json(frame))}\n\n"
    return f"event: error\ndata: {json.dumps(error_json(frame, ctx))}\n\n"


class SseResponse(StreamingResponse):
    """``text/event-stream`` that closes its body however the send ends.

    A write to a dead socket raises, and Starlette then leaves the body generator suspended at
    its ``yield`` until the garbage collector finalises it. Closing it here makes the stream
    learn of the disconnect at once (LD-18); on a finished generator it does nothing.
    """

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Send the stream, then close the body iterator."""
        try:
            await super().__call__(scope, receive, send)
        finally:
            aclose = getattr(self.body_iterator, "aclose", None)
            if aclose is not None:
                await aclose()


def supports_disconnect_probe(scope: Scope) -> bool:
    """Whether ``Request.is_disconnected`` may be polled while the response streams.

    From ASGI 2.4 Starlette runs no disconnect listener, so the connection is ours to probe;
    before it, Starlette's own listener owns ``receive`` and cancels the body on a drop.
    """
    version = str(scope.get("asgi", {}).get("spec_version", "2.0"))
    try:
        return tuple(int(x) for x in version.split(".")) >= (2, 4)
    except ValueError:
        return False
