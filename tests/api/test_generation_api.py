"""R-2, R-3 and R-4 over HTTP: the SSE contract (§5.6), the HTTP-vs-frame boundary (API-6)."""

from __future__ import annotations

import json
from collections.abc import Iterator
from dataclasses import replace
from typing import Any
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from tests.support_generate import SCHEMA, TENANT, TENDER, USER, GenRig, section_profile
from tests.support_retrieval import chunk

from rag.application.shared.gateway import GatewayQuotaRefused, GatewayUnavailable
from rag.domain.packs.generation import EligibilityDecl
from rag.domain.shared.enums import EligibilityState
from rag.infrastructure.config import Settings
from rag.presentation.generate_dependencies import GenerateServices, get_generate
from rag.presentation.main import create_app

HEADERS = {"x-tenant-id": str(TENANT), "x-user-id": str(USER)}


class Harness:
    """The real app over fake-backed generate services."""

    def __init__(self, settings: Settings, rig: GenRig) -> None:
        self.rig = rig
        services = GenerateServices(
            rig.answer, rig.draft, rig.extract, rig.admit, rig.lifecycle, rig.complete,
            rig.verify, rig.cfg, rig.engine,
        )  # fmt: skip
        self.app = create_app(settings)
        self.app.dependency_overrides[get_generate] = lambda: services
        self.client = TestClient(self.app)

    def post(self, path: str, body: dict[str, Any], **headers: str) -> Any:
        return self.client.post(path, json=body, headers={**HEADERS, **headers})


def harness(settings: Settings, **kw: Any) -> Harness:
    rig = GenRig(**kw)
    rig.dense.hits = [chunk(1, text="Contract value EUR 4,820,000 excl VAT."), chunk(2)]
    rig.lexical.hits = []
    return Harness(settings, rig)


@pytest.fixture
def h(settings: Settings) -> Iterator[Harness]:
    yield harness(settings)


def events(text: str) -> list[tuple[str, Any]]:
    """Parse an SSE body into ``(event, data)``; comment lines come back as ``(":", text)``."""
    out: list[tuple[str, Any]] = []
    for block in filter(None, text.split("\n\n")):
        if block.startswith(":"):
            out.append((":", block))
            continue
        lines = dict(line.split(": ", 1) for line in block.split("\n"))
        out.append((lines["event"], json.loads(lines["data"])))
    return out


ANSWER = {"question": "What access is specified for crane erection?"}
DRAFT = {
    "tender_id": str(TENDER),
    "tender_section_ref": "sec-4.2",
    "section_type": "method_statement",
    "user_prompt": "Cover the night-work restriction.",
}


def drawing_ok(h: Harness) -> None:
    h.rig.packs.bundle = replace(
        h.rig.packs.bundle,
        domain_profile=replace(h.rig.packs.bundle.domain_profile, required_inputs={}),
    )


def test_r2_streams_draft_events_then_one_verdict_in_event_stream(h: Harness) -> None:
    r = h.post("/api/v1/answers", ANSWER)
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/event-stream")
    assert r.headers["cache-control"] == "no-cache"
    ev = events(r.text)
    assert [e for e, _ in ev] == ["draft", "draft", "verdict"]
    assert ev[0][1] == {"delta": h.rig.stream_gw.deltas[0], "citation_refs": ["c-01"]}
    v = ev[-1][1]
    assert v["evidence_tier"] == "SUPPORTED" and v["delivery"] == "delivered"
    assert v["version_no"] == 1 and v["retrieval_set_id"] and v["artefact_version_id"]
    assert v["groundedness"]["supported"] == 2 and v["groundedness"]["tiers_run"] == [1, 2]
    assert v["completeness"] == {"verdict": "not_computed", "missed_items": []}
    assert v["eligibility"]["state"] == "GENERATABLE" and v["eligibility"]["clamped"] is False
    assert v["degradation_markers"] == [] and v["citations"][0]["outcome"] == "supported"
    assert "delta" not in json.dumps(v)  # the verdict carries no generated text (API-13)


def test_r2_api5_eligibility_clamp_rides_the_verdict(settings: Settings) -> None:
    h = harness(
        settings,
        packs_kw={
            "eligibility": {"answer": EligibilityDecl(EligibilityState.PARTIALLY_GENERATABLE)}
        },
    )
    v = events(h.post("/api/v1/answers", {**ANSWER, "requested_mode": "automation"}).text)[-1][1]
    assert v["eligibility"]["clamped"] is True
    assert (v["eligibility"]["requested_mode"], v["eligibility"]["effective_mode"]) == (
        "automation",
        "assistance",
    )
    assert v["eligibility"]["clamp_reason"] == "profile_withholds_unattended_completion"


def test_the_idempotency_key_header_replays_as_a_single_verdict_frame(h: Harness) -> None:
    key = {"Idempotency-Key": "abc-123"}
    first = events(h.post("/api/v1/answers", ANSWER, **key).text)
    again = events(h.post("/api/v1/answers", ANSWER, **key).text)
    assert [e for e, _ in again] == ["verdict"] and again[0][1]["replay"] is True
    assert again[0][1]["artefact_version_id"] == first[-1][1]["artefact_version_id"]
    clash = h.post("/api/v1/answers", {"question": "different?"}, **key)
    assert clash.status_code == 409 and clash.json()["errorCode"] == "IDEMPOTENCY_KEY_CONFLICT"
    assert clash.headers["content-type"].startswith("application/problem+json")


def test_an_empty_idempotency_key_is_a_validation_error(h: Harness) -> None:
    r = h.post("/api/v1/answers", ANSWER, **{"Idempotency-Key": ""})
    assert r.status_code == 400 and r.json()["errorCode"] == "VALIDATION_FAILED"


# --- the HTTP-versus-frame boundary (API-6) ----------------------------------------------------


def test_quota_refusal_at_dispatch_is_an_http_429_with_retry_after(h: Harness) -> None:
    h.rig.stream_gw.before_first = GatewayQuotaRefused(30)
    r = h.post("/api/v1/answers", ANSWER)
    assert r.status_code == 429 and r.headers["retry-after"] == "30"
    assert r.json()["errorCode"] == "QUOTA_EXHAUSTED"
    assert r.headers["content-type"].startswith("application/problem+json")


@pytest.mark.parametrize(
    ("arrange", "status", "code"),
    [
        (lambda h: setattr(h.rig.probe, "down", True), 503, "ARTEFACT_STORE_UNWRITABLE"),
        (lambda h: h.rig.gate.closed.add(TENDER), 409, "TENDER_CLOSED"),
        (lambda h: setattr(h.rig.stream_gw, "before_first", GatewayUnavailable("x")), 503,
         "INFERENCE_UNAVAILABLE"),
    ],
)  # fmt: skip
def test_failures_before_the_first_byte_are_http_problems(
    h: Harness, arrange, status, code
) -> None:  # type: ignore[no-untyped-def]
    arrange(h)
    r = h.post("/api/v1/answers", {**ANSWER, "scope": {"tender_id": str(TENDER)}})
    assert (r.status_code, r.json()["errorCode"]) == (status, code)


def test_r3_required_input_missing_is_http_422_before_the_first_frame(h: Harness) -> None:
    r = h.post("/api/v1/sections/drafts", DRAFT)
    assert r.status_code == 422 and r.json()["errorCode"] == "REQUIRED_INPUT_MISSING"


def test_a_failure_after_the_first_frame_is_the_terminal_error_frame_on_a_200(h: Harness) -> None:
    h.rig.stream_gw.fail_after = (1, GatewayUnavailable("dropped"))
    r = h.post("/api/v1/answers", ANSWER)
    assert r.status_code == 200  # committed by the first frame
    ev = events(r.text)
    assert [e for e, _ in ev] == ["draft", "error"]
    err = ev[-1][1]
    assert err["errorCode"] == "INFERENCE_UNAVAILABLE" and err["status"] == 503
    assert err["type"].endswith("/retrieval-generation/inference-unavailable")
    assert err["instance"] == "/api/v1/answers" and err["tenantId"] == str(TENANT)
    assert set(err) >= {"title", "detail", "timestamp"}  # traceId only when the request has one


def test_a_completion_failure_is_the_internal_error_frame(h: Harness) -> None:
    h.rig.store.fail_put = True
    ev = events(h.post("/api/v1/answers", ANSWER).text)
    err = ev[-1][1]
    assert ev[-1][0] == "error" and err["errorCode"] == "INTERNAL_ERROR" and err["status"] == 500
    assert err["type"].endswith("/platform/internal-error")


# --- the blocking compliance path (API-7) ------------------------------------------------------


def test_compliance_answers_in_the_same_media_type_with_only_the_terminal_frame(
    settings: Settings,
) -> None:
    profile = section_profile(compliance=True, disclaimer_ref="disc-1")
    h = harness(settings, profile=profile)
    drawing_ok(h)
    r = h.post("/api/v1/sections/drafts", DRAFT)
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/event-stream")
    ev = events(r.text)
    assert [e for e, _ in ev] == ["verdict"]  # no phase 1
    assert 0 in ev[0][1]["groundedness"]["tiers_run"] and ev[0][1]["disclaimer_ref"] == "disc-1"


def test_the_heartbeat_comment_commits_the_blocking_path(settings: Settings) -> None:
    import asyncio

    profile = section_profile(compliance=True)
    h = harness(settings, profile=profile, heartbeat=0.02)
    drawing_ok(h)
    h.rig.stream_gw.hold = asyncio.Event()

    async def release() -> None:
        await asyncio.sleep(0.1)
        h.rig.stream_gw.hold.set()  # type: ignore[union-attr]

    # the model is held past the heartbeat interval; release it from the app's own loop
    original = h.rig.stream_gw.generate_stream

    async def releasing(*a, **kw):  # type: ignore[no-untyped-def]
        asyncio.get_running_loop().call_later(0.1, h.rig.stream_gw.hold.set)  # type: ignore[union-attr]
        async for c in original(*a, **kw):
            yield c

    h.rig.stream_gw.generate_stream = releasing  # type: ignore[method-assign]
    ev = events(h.post("/api/v1/sections/drafts", DRAFT).text)
    assert ev[0][0] == ":" and ev[-1][0] == "verdict"  # comment lines, then the terminal frame


# --- R-4 ---------------------------------------------------------------------------------------

EXTRACT = {"tender_id": str(TENDER), "schema_ref": SCHEMA.ref}


def test_r4_returns_json_with_fields_and_the_trigger_is_accepted(h: Harness) -> None:
    r = h.post("/api/v1/extractions", {**EXTRACT, "trigger_document_version_id": "dv-1"})
    assert r.status_code == 200 and r.headers["content-type"].startswith("application/json")
    body = r.json()
    assert body["schema_version"] == "dp-civil/summary@2.1.0"
    assert body["fields"]["contract_value"]["value"] == 4820000
    assert body["fields"]["region"]["gap"] is True
    assert body["eligibility"]["state"] == "GENERATABLE" and body["retrieval_set_id"]
    assert body["artefact_version_id"] and body["evidence_tier"] == "SUPPORTED"


def test_r4_is_206_where_a_marker_reduced_its_quality(h: Harness) -> None:
    h.rig.packs.down = True
    h.rig.packs.bundle = (
        h.rig.packs.bundle
    )  # the platform default has no schema: use a degraded set
    h.rig.dense.error = None
    from rag.application.retrieval.ports import SearchHalfError

    h.rig.lexical.error = SearchHalfError("down")  # one half down: lexical-only is not this; dense
    h.rig.packs.down = False
    r = h.post("/api/v1/extractions", EXTRACT)
    assert r.status_code == 206 and r.json()["degradation_markers"]


def test_r4_errors_are_problems(h: Harness) -> None:
    h.rig.stream_gw.extract_text = json.dumps({"fields": {"contract_value": {"value": "x"}}})
    r = h.post("/api/v1/extractions", EXTRACT)
    assert r.status_code == 422 and r.json()["errorCode"] == "EXTRACTION_SCHEMA_VIOLATION"
    h2 = harness(h.app.state.settings)
    h2.rig.stream_gw.extract_error = GatewayQuotaRefused(9)
    r = h2.post("/api/v1/extractions", EXTRACT)
    assert r.status_code == 429 and r.headers["retry-after"] == "9"


# --- validation and identity -------------------------------------------------------------------


@pytest.mark.parametrize(
    ("path", "body"),
    [
        ("/api/v1/answers", {"question": ""}),
        ("/api/v1/answers", {"question": "x" * 4001}),
        ("/api/v1/answers", {"question": "q", "requested_mode": "turbo"}),
        ("/api/v1/sections/drafts", {**DRAFT, "user_prompt": "x" * 8001}),
        ("/api/v1/sections/drafts", {k: v for k, v in DRAFT.items() if k != "tender_id"}),
        ("/api/v1/sections/drafts", {**DRAFT, "tender_section_ref": "x" * 257}),
        ("/api/v1/extractions", {"tender_id": "not-a-uuid", "schema_ref": "s"}),
        ("/api/v1/extractions", {**EXTRACT, "document_ids": [str(uuid4())] * 101}),
    ],
)
def test_bounds_are_validation_failures(h: Harness, path: str, body: dict[str, Any]) -> None:
    r = h.post(path, body)
    assert r.status_code == 400 and r.json()["errorCode"] == "VALIDATION_FAILED"


@pytest.mark.parametrize(
    "path", ["/api/v1/answers", "/api/v1/sections/drafts", "/api/v1/extractions"]
)
def test_routes_require_authentication(h: Harness, path: str) -> None:
    r = h.client.post(path, json={})
    assert r.status_code == 401


def test_a_tenant_id_in_the_body_is_never_the_authority(h: Harness) -> None:
    """API-1 / API-2: the run is written for the header's tenant whatever the body says."""
    other = str(uuid4())
    h.post("/api/v1/answers", {**ANSWER, "tenant_id": other, "user_id": other})
    assert h.rig.repo.run.tenant_id == TENANT and h.rig.repo.run.principal_id == USER
