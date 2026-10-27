"""W-1 to W-8 and A-5 to A-7 over HTTP: bodies, status codes, headers, problems, SSE."""

from __future__ import annotations

import json
from collections.abc import Iterator
from dataclasses import replace
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from tests.api.test_app import _api_routes
from tests.api.test_generation_api import events
from tests.support_retrieval import chunk
from tests.support_workflows import (
    SCHEMA,
    TENANT,
    TENDER,
    USER,
    WfRig,
    engine_down,
    seed_document,
)
from tests.usecase.test_regeneration import CONFLICT, seed

from rag.application.workflows.bridges import OrphanRestarter
from rag.domain.shared.enums import ArtefactKind, OutlineState, RunStatus
from rag.infrastructure.config import Settings
from rag.presentation.main import create_app
from rag.presentation.workflows_dependencies import WorkflowServices, get_workflows

HEADERS = {"x-tenant-id": str(TENANT), "x-user-id": str(USER)}
OPERATOR = {**HEADERS, "x-tenant-roles": "platform_operator"}
W1 = {"tender_id": str(TENDER), "capability": "method_statement", "outline_gate": True}
KEY = {"Idempotency-Key": "9f3c"}


class Harness:
    """The real app over fake-backed workflow services."""

    def __init__(self, settings: Settings) -> None:
        self.rig = r = WfRig()
        r.base.dense.hits = [chunk(1, text="Crane erection access."), chunk(2)]
        r.base.lexical.hits = []
        services = WorkflowServices(
            client=r.workflows, start=r.start, start_extraction=r.start_extraction,
            state=r.state, approve=r.approve, revise=r.revise, cancel=r.cancel, resume=r.resume,
            rematerialise=r.rematerialise, regenerate_document=r.regenerate_document,
            regenerate_section=r.regenerate_section, resolve_conflict=r.resolve_conflict,
            restarter=OrphanRestarter(r.workflows, r.queues), resumer=r.resumer,
        )  # fmt: skip
        self.app = create_app(settings)
        self.app.dependency_overrides[get_workflows] = lambda: services
        self.client = TestClient(self.app)

    def post(self, path: str, body: Any = None, headers: dict[str, str] | None = None) -> Any:
        return self.client.post(path, json=body, headers=headers or HEADERS)

    def get(self, path: str) -> Any:
        return self.client.get(path, headers=HEADERS)


@pytest.fixture
def h(settings: Settings) -> Iterator[Harness]:
    yield Harness(settings)


def problem(r: Any, status: int, code: str) -> dict[str, Any]:
    assert r.status_code == status, r.text
    assert r.headers["content-type"].startswith("application/problem+json")
    body = r.json()
    assert body["errorCode"] == code
    return body  # type: ignore[no-any-return]


# =============================== W-1 / W-6 ========================================================
def test_w1_202_with_the_handle_pins_and_eligibility(h: Harness) -> None:
    r = h.post("/api/v1/generations", W1, {**HEADERS, **KEY})
    assert r.status_code == 202
    body = r.json()
    assert set(body) == {
        "generation_id", "artefact_id", "status", "eligibility",
        "domain_profile_version", "language_pack_version",
    }  # fmt: skip
    assert body["status"] == "pending" and body["domain_profile_version"] == "dp-1"
    assert body["eligibility"]["state"] == "GENERATABLE"
    assert "scope" not in json.dumps(body) and "token" not in json.dumps(body)  # no ScopeToken
    assert h.rig.workflows.starts[0].workflow_id == body["artefact_id"]  # the handle's artefact


def test_w1_idempotency_key_is_required(h: Harness) -> None:
    r = h.post("/api/v1/generations", W1)
    assert r.status_code == 400 and r.json()["errorCode"] == "VALIDATION_FAILED"
    assert h.rig.world.runs == {}


def test_w1_replay_is_200_and_a_different_body_is_409(h: Harness) -> None:
    first = h.post("/api/v1/generations", W1, {**HEADERS, **KEY}).json()
    run = h.rig.world.runs[UUID(first["generation_id"])]
    h.rig.world.runs[run.id] = replace(run, status=RunStatus.GENERATING)
    again = h.post("/api/v1/generations", W1, {**HEADERS, **KEY})
    assert again.status_code == 200 and again.json()["generation_id"] == first["generation_id"]
    clash = h.post("/api/v1/generations", {**W1, "capability": "other"}, {**HEADERS, **KEY})
    problem(clash, 409, "IDEMPOTENCY_KEY_CONFLICT")


def test_w1_engine_unreachable_is_503_and_the_row_remains(h: Harness) -> None:
    h.rig.workflows.fail_start = engine_down()
    p = problem(
        h.post("/api/v1/generations", W1, {**HEADERS, **KEY}), 503, "WORKFLOW_ENGINE_UNAVAILABLE"
    )
    assert p["type"].endswith("/workflow-engine-unavailable")
    (run,) = h.rig.world.runs.values()
    assert run.status is RunStatus.PENDING  # recoverable (ASYNC-2)


def test_w1_validation_and_auth(h: Harness) -> None:
    bad = h.post("/api/v1/generations", {**W1, "capability": ""}, {**HEADERS, **KEY})
    assert bad.status_code == 400
    assert h.client.post("/api/v1/generations", json=W1, headers=KEY).status_code == 401


def test_w6_has_the_same_shape_with_schema_ref(h: Harness) -> None:
    body = {"tender_id": str(TENDER), "schema_ref": SCHEMA.ref}
    assert h.post("/api/v1/extractions/async", body, HEADERS).status_code == 400  # key required
    r = h.post("/api/v1/extractions/async", body, {**HEADERS, **KEY})
    assert r.status_code == 202 and r.json()["status"] == "pending"
    assert h.rig.workflows.starts[0].workflow_type == "ExtractionWorkflow"
    assert h.post("/api/v1/extractions/async", body, {**HEADERS, **KEY}).status_code == 200
    bad = h.post(
        "/api/v1/extractions/async",
        {**body, "schema_ref": "nope"},
        {**HEADERS, "Idempotency-Key": "k2"},
    )
    problem(bad, 422, "PACK_INVALID")


# =============================== W-2 ==============================================================
def test_w2_state_body(h: Harness) -> None:
    _, run, _ = seed_document(h.rig.world, sections=3, complete=1)
    r = h.get(f"/api/v1/generations/{run.id}")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "awaiting_outline_approval"
    assert body["outline"]["state"] == "ready" and body["outline"]["version"] == 1
    assert body["progress"] == {"total": 3, "complete": 1, "gap": 0, "failed": 0}
    assert body["sections"][0]["state"] == "complete" and body["sections"][0]["evidence_tier"]
    assert body["coherence"]["verdict"] == "not_computed" and body["scope_narrowed"] is False
    assert body["established_facts"] == {}


def test_w2_unknown_generation_is_404(h: Harness) -> None:
    problem(h.get(f"/api/v1/generations/{uuid4()}"), 404, "ARTEFACT_NOT_FOUND")


# =============================== W-3, W-4, W-5, W-7 ===============================================
def test_w3_w4_gate_routes(h: Harness) -> None:
    _, run, _ = seed_document(h.rig.world, outline_version=2)
    base = f"/api/v1/generations/{run.id}"
    stale = h.post(f"{base}/outline/approve", {"outline_version": 1})
    problem(stale, 409, "WORKFLOW_STATE_CONFLICT")
    assert h.post(f"{base}/outline/approve", {}).status_code == 400  # outline_version is required
    rev = h.post(
        f"{base}/outline", {"outline_version": 2, "sections": [{"ref": "a", "type": "intro"}]}
    )
    assert rev.status_code == 202
    assert rev.json()["status"] == "outline_building" and rev.json()["outline_version"] == 3
    assert rev.json()["outline_state"] == "revising"


def test_w3_approve_202(h: Harness) -> None:
    _, run, _ = seed_document(h.rig.world)
    r = h.post(f"/api/v1/generations/{run.id}/outline/approve", {"outline_version": 1})
    assert r.status_code == 202 and r.json()["status"] == "generating"
    assert r.json()["outline_state"] == "approved"


def test_w5_cancel_with_and_without_a_body(h: Harness) -> None:
    _, run, _ = seed_document(h.rig.world)
    long = h.post(f"/api/v1/generations/{run.id}/cancel", {"reason": "x" * 257})
    assert long.status_code == 400  # at most 256 characters
    r = h.post(f"/api/v1/generations/{run.id}/cancel", {"reason": "superseded"})
    assert r.status_code == 202 and r.json()["status"] == "cancelled"
    _, run2, _ = seed_document(h.rig.world)
    assert h.post(f"/api/v1/generations/{run2.id}/cancel").status_code == 202  # body optional


def test_w7_resume(h: Harness) -> None:
    _, run, _ = seed_document(
        h.rig.world, status=RunStatus.SUSPENDED, outline_state=OutlineState.APPROVED
    )
    assert h.post(f"/api/v1/generations/{run.id}/resume", {"x": 1}).status_code == 400  # body is {}
    r = h.post(f"/api/v1/generations/{run.id}/resume", {})
    assert r.status_code == 202 and r.json()["status"] == "generating"
    problem(h.post(f"/api/v1/generations/{run.id}/resume", {}), 409, "WORKFLOW_STATE_CONFLICT")


def test_signal_routes_hide_a_run_outside_scope_as_404(h: Harness) -> None:
    from rag.domain.shared.errors import DomainError, ErrorCode

    _, run, _ = seed_document(h.rig.world)
    h.rig.scope.error = DomainError(ErrorCode.COLLECTION_NOT_FOUND)
    for path, body in (
        ("outline/approve", {"outline_version": 1}),
        ("outline", {"outline_version": 1}),
        ("cancel", {}),
        ("resume", {}),
    ):
        problem(h.post(f"/api/v1/generations/{run.id}/{path}", body), 404, "ARTEFACT_NOT_FOUND")


# =============================== W-8 ==============================================================
def test_w8_is_on_the_operator_prefix_and_needs_platform_operator(h: Harness) -> None:
    _, run, _ = seed_document(h.rig.world)
    path = f"/api/v1/operator/generations/{run.id}/rematerialise"
    problem(h.post(path, {"ticket_ref": "OPS-1"}), 403, "OPERATOR_REQUIRED")
    assert h.post(path, {}, OPERATOR).status_code == 400  # ticket_ref required
    assert h.post(path, {"ticket_ref": "T" * 129}, OPERATOR).status_code == 400
    r = h.post(path, {"ticket_ref": "OPS-1234", "reason": "restore"}, OPERATOR)
    assert r.status_code == 202
    body = r.json()
    assert body["generation_id"] == str(run.id) and body["workflow_id"] == run.workflow_id
    assert body["rematerialised_at"]
    problem(h.post(path, {"ticket_ref": "OPS-1234"}, OPERATOR), 409, "WORKFLOW_STATE_CONFLICT")


def test_api14_no_operator_route_exists_on_the_tenant_prefix(h: Harness) -> None:
    paths = {p for p, _ in _api_routes(h.app)}
    operator = {p for p in paths if "rematerialise" in p}
    assert operator == {"/api/v1/operator/generations/{generation_id}/rematerialise"}
    assert not any(p.startswith("/api/v1/operator") for p in paths - operator)


# =============================== A-5 ==============================================================
def test_a5_202_then_200_for_a_repeat_and_idempotency_key_is_ignored(h: Harness) -> None:
    doc = seed(h.rig)
    path = f"/api/v1/artefacts/{doc.artefact.id}/regenerate"
    body = {"base_version_id": str(doc.version.id), "instruction": "Tighten it."}
    r = h.post(path, body, {**HEADERS, **KEY})
    assert r.status_code == 202 and r.json()["status"] == "pending"
    (run,) = h.rig.world.runs.values()
    assert run.idempotency_key is None  # accepted and ignored (RUN-3 does not apply)
    assert run.workflow_id == f"regen:{doc.version.id}"
    again = h.post(path, body, {**HEADERS, "Idempotency-Key": "another"})
    assert again.status_code == 200 and again.json()["generation_id"] == r.json()["generation_id"]
    assert len(h.rig.world.runs) == 1


def test_a5_guards_and_validation(h: Harness) -> None:
    doc = seed(h.rig)
    path = f"/api/v1/artefacts/{doc.artefact.id}/regenerate"
    assert h.post(path, {}).status_code == 400  # base_version_id is required
    # review comments are A-6's: A-5's run input is stored (REGEN-3), so the field is refused
    refused = h.post(path, {"base_version_id": str(doc.version.id), "review_context": {"a": 1}})
    assert refused.status_code == 400
    problem(h.post(path, {"base_version_id": str(uuid4())}), 409, "OPTIMISTIC_LOCK_CONFLICT")
    problem(
        h.post(f"/api/v1/artefacts/{uuid4()}/regenerate", {"base_version_id": str(doc.version.id)}),
        404,
        "ARTEFACT_NOT_FOUND",
    )
    h.rig.workflows.available = False
    problem(
        h.post(path, {"base_version_id": str(doc.version.id)}), 503, "WORKFLOW_ENGINE_UNAVAILABLE"
    )


# =============================== A-6 ==============================================================
def a6_path(doc: Any, ref: str = "sec-1") -> str:
    return f"/api/v1/artefacts/{doc.artefact.id}/sections/{ref}/regenerate"


def test_a6_streams_like_r3_and_the_verdict_names_both_versions(h: Harness) -> None:
    doc = seed(h.rig)
    r = h.post(
        a6_path(doc), {"base_version_id": str(doc.sv1.id), "instruction": "Cite the restriction."}
    )
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/event-stream")
    ev = events(r.text)
    assert [e for e, _ in ev] == ["draft", "draft", "verdict"]
    v = ev[-1][1]
    assert v["prior_section_version_id"] == str(doc.sv1.id) and v["section_version_id"]
    assert v["artefact_version_id"] is None and v["version_no"] is None  # REGEN-8
    assert v["evidence_tier"] and v["groundedness"]["tiers_run"]  # REGEN-7
    assert "delta" not in json.dumps(v)  # the verdict carries no generated text (API-13)


def test_a6_a_stale_base_is_an_http_409_before_any_frame(h: Harness) -> None:
    doc = seed(h.rig)
    r = h.post(a6_path(doc), {"base_version_id": str(uuid4())})
    problem(r, 409, "OPTIMISTIC_LOCK_CONFLICT")
    assert not r.headers["content-type"].startswith("text/event-stream")


def test_a6_a_fact_conflict_is_a_single_verdict_frame_carrying_the_choice(h: Harness) -> None:
    doc = seed(h.rig)
    h.rig.gw.instruction_text = CONFLICT
    r = h.post(a6_path(doc), {"base_version_id": str(doc.sv1.id), "instruction": "Use a hoist."})
    ev = events(r.text)
    assert [e for e, _ in ev] == ["verdict"]
    c = ev[0][1]["fact_conflict"]
    assert c["fact_key"] == "access_method" and c["established_value"] == "crane"
    assert c["instruction_value"] == "hoist" and c["conflict_id"]
    assert sorted(c["choices"]) == ["dismiss", "keep_established", "update_fact"]
    assert "section_version_id" not in ev[0][1]


def test_a6_validation(h: Harness) -> None:
    doc = seed(h.rig)
    assert h.post(a6_path(doc), {}).status_code == 400
    assert h.post(a6_path(doc), {"base_version_id": "nope"}).status_code == 400
    long = {"base_version_id": str(doc.sv1.id), "instruction": "x" * 2001}
    assert h.post(a6_path(doc), long).status_code == 400
    huge = {"base_version_id": str(doc.sv1.id), "review_context": {"c": "y" * 9000}}
    assert h.post(a6_path(doc), huge).status_code == 400  # REGEN-3 input is bounded
    problem(
        h.post(a6_path(doc, "missing"), {"base_version_id": str(doc.sv1.id)}),
        404,
        "ARTEFACT_NOT_FOUND",
    )


# =============================== A-7 ==============================================================
def a7_path(doc: Any, ref: str = "sec-1") -> str:
    return f"/api/v1/artefacts/{doc.artefact.id}/sections/{ref}/fact-conflict-resolution"


def test_a7_resolve_then_a_409_for_the_second_reviewer(h: Harness) -> None:
    doc = seed(h.rig)
    h.rig.gw.instruction_text = CONFLICT
    ev = events(
        h.post(a6_path(doc), {"base_version_id": str(doc.sv1.id), "instruction": "hoist"}).text
    )
    cid = ev[0][1]["fact_conflict"]["conflict_id"]
    ok = h.post(a7_path(doc), {"conflict_id": cid, "resolution": "update_fact"})
    assert ok.status_code == 200
    assert ok.json() == {
        "conflict_id": cid,
        "resolution": "update_fact",
        "proceeds": True,
        "flagged_section_ids": [str(doc.s2.id)],
    }
    second = h.post(a7_path(doc), {"conflict_id": cid, "resolution": "keep_established"})
    assert problem(second, 409, "WORKFLOW_STATE_CONFLICT")["resolution"] == "update_fact"


def test_a7_validation_and_not_found(h: Harness) -> None:
    doc = seed(h.rig)
    assert (
        h.post(a7_path(doc), {"conflict_id": str(uuid4()), "resolution": "maybe"}).status_code
        == 400
    )
    problem(
        h.post(a7_path(doc), {"conflict_id": str(uuid4()), "resolution": "dismiss"}),
        404,
        "FACT_CONFLICT_NOT_FOUND",
    )
    draft = seed(h.rig, kind=ArtefactKind.SECTION_DRAFT, facts=False)
    problem(
        h.post(a7_path(draft), {"conflict_id": str(uuid4()), "resolution": "dismiss"}),
        404,
        "FACT_CONFLICT_NOT_FOUND",
    )
