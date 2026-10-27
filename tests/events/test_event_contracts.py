"""§12.3 / §12.6: every published event's shape, schema, envelope and the EVT-7 rules."""

from __future__ import annotations

import json
from dataclasses import fields
from typing import Any
from uuid import UUID

import pytest
from tests.events.test_corpus_events import FakeConn
from tests.support_corpus import TENANT, TENDER

from rag.application.events import contracts as c
from rag.application.shared.events import DomainEvent
from rag.domain.shared.enums import (
    ArtefactKind,
    ArtefactOrigin,
    CoherenceVerdict,
    CompletenessVerdict,
    DeliveryState,
    EligibilityState,
    EvidenceTier,
    GenerationMode,
    OutlineState,
    RunFailureReason,
    RunStage,
    RunStatus,
    SectionState,
)
from rag.infrastructure.events import schemas
from rag.infrastructure.events.publisher import EVENT_SOURCE, OutboxEventPublisher

A, G, T, P = "8a11", "7b3e", str(TENDER), "5c1c"
RECIPIENTS = [c.Recipient("user", P)]
COMMON = dict(recipients=RECIPIENTS)

SAMPLES: dict[str, Any] = {
    c.GENERATION_STARTED: c.GenerationStarted(
        A,
        G,
        P,
        ArtefactKind.DOCUMENT,
        "technical_proposal",
        False,
        GenerationMode.AUTOMATION,
        GenerationMode.ASSISTANCE,
        EligibilityState.PARTIALLY_GENERATABLE,
        True,
        "dp-civil-en-4.2.0",
        "lp-en-2.0.1",
        tender_id=T,
        target_ref="tender:0f2a:proposal",
        clamp_reason="profile_withholds_unattended_completion",
        **COMMON,
    ),
    c.GENERATION_GAP_REPORTED: c.GenerationGapReported(
        A, P, "sec-4.2", ["bill_of_quantities"], ["standard"], ["7c31"], True, tender_id=T
    ),
    c.OUTLINE_READY: c.OutlineReady(A, P, 2, 12, "2026-10-30T08:40:00Z", tender_id=T),
    c.SECTION_COMPLETED: c.SectionCompleted(
        A,
        P,
        "sec-4.2",
        SectionState.COMPLETE,
        ArtefactOrigin.REGENERATION,
        7,
        12,
        generation_id=G,
        tender_id=T,
        section_version_id="aa10",
        evidence_tier=EvidenceTier.SUPPORTED,
        completeness=CompletenessVerdict.COMPLETE,
        retrieval_set_id="3d90",
        superseded_version_ids=[],
    ),
    c.GENERATION_PROGRESSED: c.GenerationProgressed(
        A,
        G,
        P,
        7,
        RunStatus.GENERATING,
        7,
        12,
        OutlineState.APPROVED,
        "2026-10-12T08:41:12Z",
        tender_id=T,
        stage=RunStage.STAGE2,
    ),
    c.GENERATION_COMPLETED: c.GenerationCompleted(
        A,
        "2f7d",
        4,
        P,
        ArtefactKind.DOCUMENT,
        "completion",
        ArtefactOrigin.ORIGINAL,
        EvidenceTier.PARTIAL,
        CompletenessVerdict.PARTIAL,
        DeliveryState.DELIVERED,
        12,
        generation_id=G,
        tender_id=T,
        coherence_verdict=CoherenceVerdict.CONSISTENT,
    ),
    c.GENERATION_FAILED: c.GenerationFailed(
        A,
        P,
        RunFailureReason.SCOPE_REVOKED,
        7,
        12,
        True,
        generation_id=G,
        tender_id=T,
        failed_at_stage=RunStage.STAGE2,
    ),
    c.GENERATION_CANCELLED: c.GenerationCancelled(A, G, P, "superseded", 7, 12, True, tender_id=T),
    c.ARTEFACT_REGENERATED: c.ArtefactRegenerated(
        A,
        "3c02",
        5,
        "2f7d",
        P,
        "document",
        ArtefactOrigin.REGENERATION,
        EvidenceTier.PARTIAL,
        CompletenessVerdict.COMPLETE,
        tender_id=T,
    ),
    c.EXTRACTION_COMPLETED: c.ExtractionCompleted(
        A,
        "2f7d",
        G,
        P,
        "44c1",
        "dp-civil-en/tender-summary@2.1.0",
        EvidenceTier.PARTIAL,
        tender_id=T,
        recipients=RECIPIENTS,
    ),
    c.INDEX_GENERATION_ACTIVATED: c.IndexGenerationActivated(
        "7c31", T, 13, "emb-m3-1024", "c-1.9.0", "2026-04-02T09:14:00Z", 12
    ),
    c.INDEX_GENERATION_FAILED: c.IndexGenerationFailed("7c31", T, 13, ["recall"], 12),
    c.CORPUS_CONFLICT_DETECTED: c.CorpusConflictDetected(
        "7c31",
        T,
        13,
        "2026-10-12T08:41:12Z",
        [c.ConflictEntry("submission_deadline", "high", ["9c2e:7", "41d0:31"], ["91f3", "4a0c"])],
        1,
        False,
    ),
    c.COLLECTION_PROVISIONED: c.CollectionProvisioned(
        "7c31", T, "tender", False, "provisioning", None
    ),
    c.COLLECTION_PROMOTED: c.CollectionPromoted("9b42", T, "tender", True, "provisioning", "7c31"),
    c.COLLECTION_STATUS_CHANGED: c.CollectionStatusChanged(
        "7c31", T, "tender", "active", "frozen", "tenant_suspended"
    ),
    c.PROFILE_CACHE_REFRESHED: c.ProfileCacheRefreshed(
        "dp-civil-en", "civil", "en", "sections", "4.2.0", "b1c8", from_version="4.1.0"
    ),
}


def test_seventeen_contracts_each_with_a_sample() -> None:
    """§12.6: seventeen send operations, counting the one not published in v1."""
    assert len(c.PUBLISHED) == 17 == len(SAMPLES) and set(SAMPLES) == set(c.PUBLISHED)
    assert c.INDEX_EVENT_TYPES <= set(c.PUBLISHED) and len(c.INDEX_EVENT_TYPES) == 7
    assert all(name == cls.__name__ for name, cls in c.PUBLISHED.items())


@pytest.mark.parametrize("name", sorted(SAMPLES))
def test_sample_validates_against_its_derived_schema(name: str) -> None:
    """The payload a builder emits is what the schema accepts (§12.3 examples)."""
    payload = c.payload_of(SAMPLES[name])
    schemas.validate_published(name, payload)
    json.dumps(payload)  # plain JSON: enums are strings
    assert not any(isinstance(v, c.Recipient) for v in payload.values())


@pytest.mark.parametrize("name", sorted(SAMPLES))
async def test_envelope_carries_every_mandatory_field(name: str) -> None:
    """§12.2: id (uuidv7), type PascalCase, source constant, time, tenant, subject, correlation."""
    conn = FakeConn()
    event = c.build_event(TENANT, c.tender_subject(TENDER), SAMPLES[name], correlation_id="corr-1")
    await OutboxEventPublisher().publish(conn, event)
    [(event_id, type_, body, tenant, _t, _o)] = conn.rows
    wire = json.loads(body)
    assert UUID(event_id).version == 7 and wire["id"] == event_id
    assert type_ == wire["type"] == name and name[0].isupper() and "." not in name
    assert wire["source"] == EVENT_SOURCE == "llm-rag-service"
    assert wire["time"] and wire["tenant_id"] == tenant == str(TENANT)
    assert wire["subject"] == f"tenders/{TENDER}" and wire["correlation_id"] == "corr-1"
    assert "actor" not in wire and "ip_address" not in wire and "user_agent" not in wire
    assert "specversion" not in wire and "dataschema" not in wire  # unset: off the wire


def test_unset_optional_fields_are_absent_but_declared_nulls_are_null() -> None:
    """§12.3: a section with no version has no ``section_version_id``; a first activation has a
    ``previous_generation`` of null."""
    gap = c.SectionCompleted(A, P, "s", SectionState.GAP, ArtefactOrigin.ORIGINAL, 1, 2)
    wire = c.payload_of(gap)
    for absent in (
        "section_version_id",
        "evidence_tier",
        "completeness",
        "retrieval_set_id",
        "superseded_version_ids",
        "generation_id",
    ):
        assert absent not in wire
    first = c.payload_of(c.IndexGenerationActivated("c", T, 1, "e", "k", "t"))
    assert first["previous_generation"] is None
    assert c.payload_of(SAMPLES[c.SECTION_COMPLETED])["superseded_version_ids"] == []


def test_every_generation_event_carries_recipients_and_index_events_do_not() -> None:
    """LD-83: ``recipients`` on every ``llm.generation.events`` type, an empty array by default."""
    for name in c.GENERATION_EVENT_TYPES:
        assert "recipients" in {f.name for f in fields(c.PUBLISHED[name])}, name
    assert c.payload_of(c.GenerationGapReported(A, P, "s", [], [], [], True))["recipients"] == []
    for name in c.INDEX_EVENT_TYPES:
        assert "recipients" not in {f.name for f in fields(c.PUBLISHED[name])}


FORBIDDEN = {
    "text",
    "content",
    "chunk_text",
    "prompt",
    "prose",
    "body",
    "excerpt",
    "answer",
    "query",
    "connection_id",
    "session_id",
    "socket_id",
    "sse_id",
    "approved",
    "approval",
    "approval_state",
}


@pytest.mark.parametrize("name", sorted(c.PUBLISHED))
def test_no_payload_field_can_carry_content_or_a_connection_id_or_an_approval(name: str) -> None:
    """EVT-7, ASYNC-7, EVT-11/AOB-1: the contracts have no such field to fill."""
    names = {f.name for f in fields(c.PUBLISHED[name])}
    assert not names & FORBIDDEN, names & FORBIDDEN


def test_schemas_are_open_and_in_step_with_the_committed_files() -> None:
    """EVT-9 / LD-65: ``additionalProperties: true`` everywhere, files equal their derivation."""
    committed = {p.name: p.read_text() for p in schemas.SCHEMA_DIR.glob("*.json")}
    assert committed == schemas.schema_files(), "run: python -m rag.infrastructure.events.schemas"

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            if node.get("type") == "object":
                assert node["additionalProperties"] is True
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)

    for name in c.PUBLISHED:
        walk(schemas.json_schema(name))
        assert f"{schemas.schema_stem(name)}.json" in committed


def test_snake_case_stems_follow_schemagov() -> None:
    """``SectionCompleted`` -> ``section_completed`` (§12.6)."""
    assert schemas.schema_stem("SectionCompleted") == "section_completed"
    assert schemas.schema_stem("IndexGenerationActivated") == "index_generation_activated"


def test_validation_is_fail_closed() -> None:
    """EVT-9: an unregistered type, a missing field, a bad enum value all raise."""
    with pytest.raises(ValueError, match="not a registered"):
        schemas.validate_published("SomethingNew", {})
    with pytest.raises(ValueError):
        schemas.validate_published(c.GENERATION_FAILED, {"artefact_id": "x"})
    bad = c.payload_of(SAMPLES[c.GENERATION_FAILED]) | {"reason": "because"}
    with pytest.raises(ValueError):
        schemas.validate_published(c.GENERATION_FAILED, bad)
    with pytest.raises(ValueError, match="not a registered"):
        c.build_event(TENANT, "", object.__new__(c.TenderEvent))


async def test_envelope_over_240kb_is_refused_by_enqueue() -> None:
    """OBX-2: ``CorpusConflictDetected`` is the unbounded one (CONFLICT_REPORT_MAX bounds it)."""
    many = [c.ConflictEntry("k" * 40, "high", ["c:1"] * 60, ["d"] * 60) for _ in range(400)]
    big = c.CorpusConflictDetected("c", T, 1, "t", many, 400, False)
    event = c.build_event(TENANT, c.tender_subject(TENDER), big)
    with pytest.raises(ValueError, match="exceeds safe SNS"):
        await OutboxEventPublisher().publish(FakeConn(), event)
    fits = c.build_event(TENANT, "", SAMPLES[c.CORPUS_CONFLICT_DETECTED])
    await OutboxEventPublisher().publish(FakeConn(), fits)


def test_subject_is_absent_for_the_sentinels_and_for_free_standing_answers() -> None:
    """LD-27."""
    from rag.domain.corpus.collection import REFERENCE, TENANT_WIDE

    assert c.tender_subject(TENDER) == f"tenders/{TENDER}"
    assert (
        c.tender_subject(None) == c.tender_subject(TENANT_WIDE) == c.tender_subject(REFERENCE) == ""
    )


def test_consumed_payloads_ignore_unknown_keys_and_reject_missing_fields() -> None:
    """EVENT_SCHEMA_GOVERNANCE: read the fields we need, tolerate additions."""
    purge = schemas.parse_consumed(
        c.DOCUMENT_PURGED,
        {
            "document_id": str(TENDER),
            "document_version": 3,
            "collection_id": str(TENANT),
            "added_later": 1,
        },
    )
    assert purge.document_version == 3 and purge.tenant_id is None
    with pytest.raises(ValueError):
        schemas.parse_consumed(c.DOCUMENT_PURGED, {"document_id": str(TENDER)})
    with pytest.raises(ValueError):
        schemas.parse_consumed(c.TENANT_STATE_CHANGED, {"cause": "x"})
    assert {*c.TENDER_TERMINAL_TYPES, c.TENDER_RESTRICTED, c.TENDER_PURGED} <= set(c.CONSUMED)


def test_domain_event_default_has_no_correlation() -> None:
    """Additive field: existing positional constructions are unchanged."""
    assert DomainEvent("X", TENANT, "s").correlation_id == ""
