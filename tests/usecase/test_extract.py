"""R-4 over fakes: schema resolution, provenance or gap, validation, refusals, replay."""

from __future__ import annotations

import json
from dataclasses import replace
from uuid import UUID

import pytest
from tests.support_generate import SCHEMA, TENANT, TENDER, GenRig
from tests.support_retrieval import chunk, identity

from rag.application.generation.requests import ExtractCommand
from rag.application.retrieval.scope import ScopeSpec
from rag.application.shared.gateway import GatewayQuotaRefused, GatewayUnavailable
from rag.domain.shared.enums import (
    ArtefactStatus,
    DeliveryState,
    EvidenceTier,
    RunFailureReason,
    RunStage,
    RunStatus,
)
from rag.domain.shared.errors import DomainError, ErrorCode

CMD = ExtractCommand(TENDER, SCHEMA.ref, ScopeSpec(TENDER), idempotency_key=None)
VALUE_TEXT = "Contract value EUR 4,820,000 excluding VAT, region Rotterdam."


def rig() -> GenRig:
    r = GenRig()
    r.dense.hits = [chunk(1, text=VALUE_TEXT), chunk(2, text="Unrelated paving note.")]
    r.lexical.hits = []
    return r


async def test_extraction_returns_fields_with_provenance_and_gaps_and_writes_the_artefact() -> None:
    r = rig()
    out = await r.extract.execute(identity(), replace(CMD, trigger_document_version_id="dv-9"))
    assert out.schema_version == "dp-civil/summary@2.1.0" and not out.degraded
    cv = out.fields["contract_value"]
    assert cv["value"] == 4820000 and cv["provenance"][0]["chunk_id"] == "v1:1"
    assert (
        cv["provenance"][0]["excerpt_is_translation"] is False
        and cv["provenance"][0]["source_span"] is None
    )
    assert out.fields["region"] == {
        "value": None,
        "gap": True,
        "gap_reason": "no_supporting_evidence",
    }
    assert out.evidence_tier is EvidenceTier.SUPPORTED and out.artefact_version_id is not None
    (rows,) = r.repo.completions
    assert rows.artefact_version.output_schema_version == "dp-civil/summary@2.1.0"
    assert json.loads(r.store.objects[rows.section_version.content_ref])["fields"]["contract_value"]
    assert r.repo.artefact.status is ArtefactStatus.COMPLETE
    assert rows.artefact_version.delivery is DeliveryState.NOT_APPLICABLE  # a JSON response
    assert r.repo.run.current_stage is RunStage.EXTRACT
    # GEN-10: both events in the write transaction, ExtractionCompleted echoes the trigger
    assert r.events.types() == ["GenerationStarted", "GenerationCompleted", "ExtractionCompleted"]
    done = r.events.events[-1].payload
    assert done["trigger_document_version_id"] == "dv-9" and done["generation_id"] == str(
        r.repo.run.id
    )
    assert r.stream_gw.completions[-1][0] == "extract"  # one extract call, never a stream
    assert r.stream_gw.stream_calls == []


async def test_a_value_the_cited_chunk_does_not_contain_is_unsupported_and_flagged() -> None:
    r = rig()
    r.stream_gw.extract_text = json.dumps(
        {"fields": {"contract_value": {"value": 999, "chunk_ids": ["c-01"]}}}
    )
    out = await r.extract.execute(identity(), CMD)
    assert out.evidence_tier is EvidenceTier.INSUFFICIENT  # kept, flagged, counted (VER-2)
    assert out.fields["contract_value"]["value"] == 999
    assert r.repo.completions[0].groundedness.unsupported == 1  # type: ignore[union-attr]


async def test_schema_violation_is_422_non_retryable_and_the_raw_output_is_kept() -> None:
    r = rig()
    r.stream_gw.extract_text = json.dumps(
        {"fields": {"contract_value": {"value": "ten", "chunk_ids": ["c-01"]}}}
    )
    with pytest.raises(DomainError) as exc:
        await r.extract.execute(identity(), CMD)
    assert exc.value.code is ErrorCode.EXTRACTION_SCHEMA_VIOLATION
    assert len(r.stream_gw.completions) == 1  # not retried
    raw = [k for k in r.store.objects if "/diagnostics/" in k]
    assert raw and b"ten" in r.store.objects[raw[0]]  # WP-8
    assert not r.repo.completions  # validated BEFORE the artefact is written
    assert r.repo.run.status is RunStatus.FAILED and r.repo.artefact.status is ArtefactStatus.FAILED


async def test_an_unknown_schema_ref_is_refused_before_the_shell_commits() -> None:
    r = rig()
    with pytest.raises(DomainError) as exc:
        await r.extract.execute(identity(), replace(CMD, schema_ref="dp/nope"))
    assert exc.value.code is ErrorCode.PACK_INVALID and not r.repo.runs


async def test_insufficient_evidence_is_a_200_with_every_field_a_gap_and_no_dispatch() -> None:
    r = rig()
    r.dense.hits = []
    out = await r.extract.execute(identity(), CMD)
    assert out.refusal and out.refusal["reason"] == "insufficient_evidence"
    assert all(f["gap"] for f in out.fields.values()) and out.artefact_version_id is None
    assert r.stream_gw.completions == [] and r.repo.run.status is RunStatus.CANCELLED


async def test_document_ids_narrow_within_the_resolved_scope() -> None:
    r = rig()
    other = UUID(int=424242)
    out = await r.extract.execute(identity(), replace(CMD, document_ids=(other,)))
    assert out.refusal is not None  # nothing of that document was retrieved
    r2 = rig()
    out2 = await r2.extract.execute(identity(), replace(CMD, document_ids=(UUID(int=1001),)))
    assert out2.refusal is None and out2.fields["contract_value"]["provenance"]


async def test_quota_refusal_is_429_with_retry_after_and_fails_the_run() -> None:
    r = rig()
    r.stream_gw.extract_error = GatewayQuotaRefused(45)
    with pytest.raises(DomainError) as exc:
        await r.extract.execute(identity(), CMD)
    assert exc.value.code is ErrorCode.QUOTA_EXHAUSTED and exc.value.retry_after_seconds == 45
    assert r.repo.run.failure_reason is RunFailureReason.QUOTA_EXHAUSTED


async def test_gateway_outage_is_503_inference_unavailable() -> None:
    r = rig()
    r.stream_gw.extract_error = GatewayUnavailable("x")
    with pytest.raises(DomainError) as exc:
        await r.extract.execute(identity(), CMD)
    assert exc.value.code is ErrorCode.INFERENCE_UNAVAILABLE


async def test_a_repeat_with_the_same_key_reads_the_stored_extraction_back() -> None:
    r = rig()
    cmd = replace(CMD, idempotency_key="k")
    first = await r.extract.execute(identity(), cmd)
    calls = len(r.stream_gw.completions)
    again = await r.extract.execute(identity(), cmd)
    assert again.replay and again.artefact_version_id == first.artefact_version_id
    assert again.fields == first.fields and len(r.stream_gw.completions) == calls
    with pytest.raises(DomainError) as exc:
        await r.extract.execute(
            identity(), replace(cmd, schema_ref=SCHEMA.ref, document_ids=(UUID(int=1),))
        )
    assert exc.value.code is ErrorCode.IDEMPOTENCY_KEY_CONFLICT


async def test_the_extraction_completes_even_if_the_caller_is_cancelled() -> None:
    """GEN-2 on the synchronous path: the shielded task writes the artefact regardless."""
    import asyncio

    r = rig()
    r.stream_gw.extract_hold = asyncio.Event()
    task = asyncio.create_task(r.extract.execute(identity(), CMD))
    while not r.stream_gw.completions:  # the call is dispatched and waiting
        await asyncio.sleep(0)
    task.cancel()  # the caller drops
    r.stream_gw.extract_hold.set()
    await r.settle()
    assert task.cancelled()
    assert len(r.repo.completions) == 1 and r.repo.artefact.status is ArtefactStatus.COMPLETE
    _ = TENANT
