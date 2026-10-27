"""R-1, R-6 and R-7 over HTTP: statuses, bodies, §5.2 bounds, auth, API-1."""

from __future__ import annotations

from collections.abc import Iterator
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from tests.support_retrieval import (
    TENANT,
    USER,
    Rig,
    bundle,
)

from rag.application.retrieval.classify import ProposeClassification
from rag.application.retrieval.ports import SearchHalfError
from rag.application.retrieval.search import SearchDocuments
from rag.application.shared.gateway import GatewayQuotaRefused
from rag.domain.retrieval.sufficiency import RequiredInputRule
from rag.domain.shared.errors import DomainError, ErrorCode
from rag.infrastructure.config import Settings
from rag.presentation.main import create_app
from rag.presentation.retrieval_dependencies import RetrievalServices, get_retrieval

HEADERS = {"x-tenant-id": str(TENANT), "x-user-id": str(USER)}


class Harness:
    """The real app over a fake-backed ``RetrievalServices``."""

    def __init__(self, settings: Settings) -> None:
        self.rig = Rig(refiner=True)
        self.rig.packs.bundle = bundle(
            required_inputs={"method_statement": RequiredInputRule(("drawing",))}
        )
        r = self.rig
        self.services = RetrievalServices(
            search=SearchDocuments(r.pipeline, r.recorder),
            classify=ProposeClassification(r.gw, r.packs, 1.0),
            required_input=r.required,
            pipeline=r.pipeline,
            recorder=r.recorder,
            gateway=r.gw,
            tokens=r.gw,
            packs=r.packs,
            _closers=(),  # type: ignore[arg-type]
        )
        self.app = create_app(settings)
        self.app.dependency_overrides[get_retrieval] = lambda: self.services
        self.client = TestClient(self.app)


@pytest.fixture
def h(settings: Settings) -> Iterator[Harness]:
    """A fresh harness."""
    yield Harness(settings)


def search(h: Harness, body: dict | None = None, headers: dict | None = None):  # type: ignore[no-untyped-def]
    """POST R-1."""
    return h.client.post(
        "/api/v1/search",
        json=body if body is not None else {"query": "crane access"},
        headers=headers or HEADERS,
    )


def test_r1_returns_200_with_the_verdict_chunks_refinement_and_profile(h: Harness) -> None:
    """§5.7.1 body shape; the set id resolves to a recorded set (API-9)."""
    r = search(h, {"query": "crane access", "section_type": "method_statement", "limit": 3})
    assert r.status_code == 200
    body = r.json()
    assert set(body) == {"verdict", "chunks", "refinement", "applied_profile"}
    assert (
        body["verdict"]["verdict"] == "sufficient" and body["verdict"]["degradation_markers"] == []
    )
    assert body["verdict"]["retrieval_set_id"] == str(h.rig.sets.inserted[0].retrieval_set.id)
    assert [c["rank"] for c in body["chunks"]] == [1, 2, 3]
    first = body["chunks"][0]
    assert first["purged"] is False and first["source_span"] is None and first["excerpt"]
    assert (
        body["refinement"]["outcome"] == "success"
        and body["refinement"]["original_query"] == "crane access"
    )
    assert body["applied_profile"]["rerank_out"] == 5


def test_r1_is_206_with_markers_when_a_stage_degraded(h: Harness) -> None:
    """The degraded-success code is derived from the markers, never set by hand (§6.1)."""
    h.rig.gw.rerank_error = GatewayQuotaRefused()
    r = search(h)
    assert r.status_code == 206
    assert r.json()["verdict"]["degradation_markers"] == [
        {"stage": "rerank", "reason": "quota_refused", "applied_fallback": "fused_order"}
    ]


def test_r1_omits_the_refinement_block_when_the_flag_is_off(h: Harness) -> None:
    """§5.7.1: absent (null), not ``outcome: disabled``."""
    h.rig.refiner._enabled = False  # type: ignore[attr-defined]
    assert search(h).json()["refinement"] is None


def test_r1_failed_retrieval_is_503_with_no_set_id(h: Harness) -> None:
    """MD-14: both halves down is an incident, not a body."""
    h.rig.dense.error = h.rig.lexical.error = SearchHalfError()
    r = search(h)
    assert r.status_code == 503 and r.json()["errorCode"] == "RETRIEVAL_FAILED"
    assert h.rig.sets.inserted == []


def test_r1_unwritable_set_is_503_artefact_store_unwritable(h: Harness) -> None:
    """§5.7.1: the response carries an id that must resolve."""
    h.rig.tx.fail = RuntimeError("disk")
    r = search(h)
    assert r.status_code == 503 and r.json()["errorCode"] == "ARTEFACT_STORE_UNWRITABLE"


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"query": ""},
        {"query": "x" * 4001},
        {"query": "q", "scope": {"collection_ids": [str(uuid4())] * 101}},
        {"query": "q", "scope": {"collection_ids": ["not-a-uuid"]}},
        {"query": "q", "scope": {"tender_id": "nope"}},
        {"query": "q", "scope": {"collection_types": ["secret"]}},
        {"query": "q", "limit": 0},
        {"query": "q", "section_type": "s" * 257},
        {"query": "q", "prior_refinement": {"refinement_id": str(uuid4()), "action": "liked"}},
    ],
)
def test_r1_validation_is_400_validation_failed(h: Harness, body: dict) -> None:  # type: ignore[type-arg]
    """§5.2: every bound answers 400 with the platform's code."""
    r = search(h, body)
    assert r.status_code == 400 and r.json()["errorCode"] == "VALIDATION_FAILED"


def test_r1_accepts_the_maximum_bounds(h: Harness) -> None:
    """4,000 characters and 100 collection ids are valid."""
    r = search(h, {"query": "x" * 4000, "scope": {"collection_ids": [str(uuid4())] * 100}})
    assert r.status_code == 200


def test_r1_requires_authentication_and_ignores_body_identity(h: Harness) -> None:
    """API-11 / API-1: no headers is 401; a body tenant_id changes nothing."""
    assert h.client.post("/api/v1/search", json={"query": "q"}).status_code == 401
    r = search(h, {"query": "q", "tenant_id": str(uuid4()), "user_id": str(uuid4())})
    assert r.status_code == 200
    assert h.rig.sets.inserted[0].retrieval_set.tenant_id == TENANT


def test_r1_passes_scope_to_the_resolver_and_surfaces_its_refusals(h: Harness) -> None:
    """API-3: tender_id routes; a 404/403 from scope resolution is the route's answer."""
    tender, coll = uuid4(), uuid4()
    search(h, {"query": "q", "scope": {"tender_id": str(tender), "collection_ids": [str(coll)]}})
    spec = h.rig.provider.requests[0][0]
    assert spec.tender_id == tender and spec.collection_ids == (coll,)
    for code, status in (
        (ErrorCode.COLLECTION_NOT_FOUND, 404),
        (ErrorCode.GRANT_LEVEL_INSUFFICIENT, 403),
        (ErrorCode.SCOPE_STORE_UNAVAILABLE, 503),
    ):
        h.rig.provider.error = DomainError(code)
        r = search(h)
        assert (r.status_code, r.json()["errorCode"]) == (status, code.value)


# --- R-6 ----------------------------------------------------------------------------------------


def classify(h: Harness, body: dict | None = None):  # type: ignore[no-untyped-def]
    """POST R-6."""
    return h.client.post(
        "/api/v1/classification/propose",
        json=body or {"content": "scc text", "filename": "SCC.pdf"},
        headers=HEADERS,
    )


def test_r6_returns_the_proposal_and_writes_nothing(h: Harness) -> None:
    """GEN-9: stateless. No scope resolved, no set, no refinement row."""
    h.rig.gw.complete_text = (
        '{"proposed_role":"specification","confidence":0.88,'
        '"alternatives":[{"role":"drawing","confidence":0.09}]}'
    )
    r = classify(h)
    assert r.status_code == 200
    assert r.json() == {
        "proposed_role": "specification",
        "confidence": 0.88,
        "alternatives": [{"role": "drawing", "confidence": 0.09}],
        "taxonomy_version": "tax-1",
    }
    assert not h.rig.provider.requests and not h.rig.sets.inserted and not h.rig.refinements.rows


def test_r6_refusals_have_their_own_statuses(h: Harness) -> None:
    """LD-99: 429 + Retry-After, 503 inference unavailable."""
    h.rig.gw.complete_error = GatewayQuotaRefused(9)
    r = classify(h)
    assert r.status_code == 429 and r.headers["retry-after"] == "9"
    from rag.application.shared.gateway import GatewayUnavailable

    h.rig.gw.complete_error = GatewayUnavailable()
    r = classify(h)
    assert r.status_code == 503 and r.json()["errorCode"] == "INFERENCE_UNAVAILABLE"


def test_r6_bounds_auth(h: Harness) -> None:
    """§5.2: 1 MB content; empty content is 400; anonymous is 401."""
    assert classify(h, {"content": ""}).status_code == 400
    assert classify(h, {"content": "x" * 1_000_001}).status_code == 400
    assert h.client.post("/api/v1/classification/propose", json={"content": "x"}).status_code == 401
    h.rig.gw.complete_text = '{"proposed_role":"drawing","confidence":0.5}'
    assert classify(h, {"content": "x" * 1_000_000}).status_code == 200


# --- R-7 ----------------------------------------------------------------------------------------


def test_r7_is_200_even_when_blocking(h: Harness) -> None:
    """§5.7.7: a gap report is the answer; the status is 200."""
    r = h.client.post(
        "/api/v1/required-input-checks",
        json={
            "tender_id": str(uuid4()),
            "capability": "technical_proposal",
            "section_types": ["method_statement", "other"],
        },
        headers=HEADERS,
    )
    assert r.status_code == 200
    body = r.json()
    assert body["profile_version"] == "dp-1"
    assert body["per_section"]["method_statement"] == {
        "missing_roles": ["drawing"],
        "unresolved_entities": [],
        "empty_collections": [],
        "blocking": True,
    }
    assert body["per_section"]["other"]["blocking"] is False


def test_r7_count_failure_is_503_retrieval_failed(h: Harness) -> None:
    """LD-100: no gap report, and no guess."""
    h.rig.inspector.error = SearchHalfError()
    r = h.client.post(
        "/api/v1/required-input-checks",
        json={"section_types": ["method_statement"]},
        headers=HEADERS,
    )
    assert r.status_code == 503 and r.json()["errorCode"] == "RETRIEVAL_FAILED"


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"section_types": []},
        {"section_types": ["a"] * 101},
        {"section_types": [1]},
    ],
)
def test_r7_validation(h: Harness, body: dict) -> None:  # type: ignore[type-arg]
    """§5.2: section_types bounded at 100 and required."""
    r = h.client.post("/api/v1/required-input-checks", json=body, headers=HEADERS)
    assert r.status_code == 400


def test_r7_requires_authentication(h: Harness) -> None:
    """API-11."""
    r = h.client.post("/api/v1/required-input-checks", json={"section_types": ["a"]})
    assert r.status_code == 401
