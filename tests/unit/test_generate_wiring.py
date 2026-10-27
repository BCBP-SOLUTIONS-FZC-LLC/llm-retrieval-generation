"""Wiring of R-2/R-3/R-4 and the SSE encoder: shared collaborators, frame shapes."""

from __future__ import annotations

import json
from uuid import uuid4

import pytest
from tests.api.test_lifespan import FakePool

from rag.application.generation.frames import DraftFrame, ErrorFrame, Heartbeat, VerdictFrame
from rag.domain.generation.eligibility import EligibilityVerdict
from rag.domain.shared.enums import (
    DeliveryState,
    EligibilityState,
    EvidenceTier,
    GenerationMode,
)
from rag.infrastructure.config import Settings
from rag.infrastructure.persistence.pools import ContextTenantBinder
from rag.presentation.api.public.sse import HEARTBEAT, ErrorContext, encode
from rag.presentation.dependencies import build_container

CTX = ErrorContext("https://errors.bcbp.io", "/api/v1/answers", "trace-1", "tenant-1")


def test_the_container_wires_one_flow_for_every_generation_route() -> None:
    """Admission, lifecycle, completion and the ladder are shared (the workflows slice reuses)."""
    c = build_container(Settings(), FakePool("c", []), FakePool("g", []))  # type: ignore[arg-type]
    g = c.generate
    assert g.answer._engine._admit is g.admit is g.extract._admit
    assert g.answer._engine._complete is g.complete is g.extract._complete
    assert g.answer._engine._lifecycle is g.lifecycle is g.extract._lifecycle
    assert g.answer._engine._verify is g.verify
    assert g.answer._engine._pipeline is c.retrieval.pipeline  # the same retrieval pipeline
    assert g.admit._scope is c.scope.adapter and g.admit._tokens is c.retrieval.tokens
    assert g.settings.heartbeat_seconds == 15.0 and g.settings.extraction_deadline_seconds == 90.0
    assert g.answer._engine._gw is c.retrieval.gateway  # one Gateway client (DEP-2)


def test_the_tenant_binder_is_the_pool_binding() -> None:
    from pgcommon import guc_set_from_context

    with ContextTenantBinder().bind(uuid4()):
        assert guc_set_from_context() is not None


def eligibility() -> EligibilityVerdict:
    return EligibilityVerdict(
        EligibilityState.GENERATABLE,
        GenerationMode.ASSISTANCE,
        GenerationMode.ASSISTANCE,
        False,
        "dp",
    )


def test_sse_frames_are_named_events_and_the_heartbeat_is_a_comment() -> None:
    assert encode(Heartbeat(), CTX) == HEARTBEAT == ": heartbeat\n\n"
    text = encode(DraftFrame("Hello ", ("c-01",)), CTX)
    assert text.startswith("event: draft\ndata: ") and text.endswith("\n\n")
    assert json.loads(text.split("data: ")[1]) == {"delta": "Hello ", "citation_refs": ["c-01"]}


def test_the_verdict_event_never_carries_generated_text_and_keeps_the_refusal() -> None:
    frame = VerdictFrame(
        artefact_id=uuid4(), generation_id=uuid4(), retrieval_set_id=uuid4(),
        evidence_tier=EvidenceTier.NOT_COMPUTED, eligibility=eligibility(),
        delivery=DeliveryState.NOT_APPLICABLE, refusal={"reason": "insufficient_evidence"},
    )  # fmt: skip
    data = json.loads(encode(frame, CTX).split("data: ")[1])
    assert data["refusal"] == {"reason": "insufficient_evidence"}
    assert data["artefact_version_id"] is None and data["evidence_tier"] == "NOT_COMPUTED"
    assert not {"text", "content", "delta"} & set(data)


def test_the_error_event_is_a_problem_detail_in_the_library_shape() -> None:
    data = json.loads(encode(ErrorFrame("INFERENCE_UNAVAILABLE", "down"), CTX).split("data: ")[1])
    assert data["type"] == "https://errors.bcbp.io/retrieval-generation/inference-unavailable"
    assert (data["status"], data["errorCode"], data["instance"]) == (
        503,
        "INFERENCE_UNAVAILABLE",
        "/api/v1/answers",
    )
    assert (data["traceId"], data["tenantId"]) == ("trace-1", "tenant-1")
    internal = json.loads(encode(ErrorFrame("INTERNAL_ERROR", "x"), CTX).split("data: ")[1])
    assert internal["type"] == "https://errors.bcbp.io/platform/internal-error"
    assert internal["status"] == 500


async def test_container_shutdown_drains_producers_before_closing_any_client() -> None:
    """GEN-2: closing the Gateway client under a producer would lose its artefact."""
    order: list[str] = []

    class Gen:
        async def drain(self, timeout: float) -> None:
            order.append(f"drain:{timeout}")

    class Closer:
        def __init__(self, name: str) -> None:
            self.name = name

        async def aclose(self) -> None:
            order.append(self.name)

    c = build_container(Settings(), FakePool("c", []), FakePool("g", []))  # type: ignore[arg-type]
    c.generate, c.retrieval, c.scope = Gen(), Closer("gateway"), Closer("scope")  # type: ignore[assignment]
    await c.aclose()
    assert order == ["drain:120.0", "gateway", "scope"]


async def test_the_sse_response_closes_the_body_when_the_socket_dies() -> None:
    """A write to a dead socket raises; the stream must learn of it now, not at GC (LD-18)."""
    import asyncio

    from fastapi import FastAPI
    from starlette.requests import ClientDisconnect, Request
    from tests.support_generate import GenRig
    from tests.support_retrieval import identity

    from rag.application.generation.requests import AnswerCommand
    from rag.application.retrieval.scope import ScopeSpec
    from rag.domain.shared.enums import DeliveryState
    from rag.presentation.api.public.generation import _sse

    rig = GenRig(heartbeat=0.02)  # the model is held, so the next write is a heartbeat
    rig.stream_gw.hold = asyncio.Event()
    stream = await rig.answer.execute(identity(), AnswerCommand("q?", ScopeSpec()))
    app = FastAPI()
    app.state.settings = Settings()
    scope = {"type": "http", "method": "POST", "path": "/api/v1/answers", "headers": [],
             "app": app, "asgi": {"spec_version": "2.4"}}  # fmt: skip
    response = _sse(stream, Request(scope), identity())
    sent: list[str] = []

    async def send(message: dict[str, object]) -> None:
        sent.append(str(message["type"]))
        if len(sent) == 3:  # the status, the first draft frame, a heartbeat: then the socket dies
            raise OSError("broken pipe")

    async def receive() -> dict[str, object]:
        return {"type": "http.disconnect"}

    with pytest.raises(ClientDisconnect):
        await response(scope, receive, send)
    rig.stream_gw.hold.set()
    await rig.settle()
    assert rig.repo.completions[0].artefact_version.delivery is DeliveryState.DISCONNECTED


def test_the_disconnect_probe_is_offered_only_from_asgi_2_4() -> None:
    from rag.presentation.api.public.sse import supports_disconnect_probe

    assert supports_disconnect_probe({"asgi": {"spec_version": "2.4"}})
    assert supports_disconnect_probe({"asgi": {"spec_version": "2.5"}})
    assert not supports_disconnect_probe({"asgi": {"spec_version": "2.3"}})
    assert not supports_disconnect_probe({})
    assert not supports_disconnect_probe({"asgi": {"spec_version": "weird"}})
