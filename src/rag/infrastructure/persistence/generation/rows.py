"""asyncpg record -> domain entity mapping. jsonb arrives as ``str`` (no codec is registered)."""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

from rag.domain.generation.artefact import Artefact, ArtefactVersion
from rag.domain.generation.audit import AuditEntry, AuditEntryDraft
from rag.domain.generation.retrieval import (
    RetrievalChunk,
    RetrievalSet,
    RetrievalSetPurge,
)
from rag.domain.generation.section import ArtefactSection, SectionVersion
from rag.domain.generation.verification import (
    ClaimVerification,
    CompletenessCheck,
    GroundednessReport,
)
from rag.domain.shared.enums import (
    ArtefactArchiveState,
    ArtefactKind,
    ArtefactOrigin,
    ArtefactStatus,
    AuditActorType,
    ClaimOutcome,
    CompletenessVerdict,
    DeliveryState,
    EligibilityState,
    EvidenceTier,
    GenerationMode,
    GrantLevel,
    RetrievalVerdict,
    RunFailureReason,
    SectionState,
)

type Row = Mapping[str, Any]


def load_json(value: Any) -> Any:
    """Decode a jsonb column (``str``) or pass an already-decoded value through."""
    return json.loads(value) if isinstance(value, str | bytes) else value


def dump_json(value: Any) -> str:
    """Encode for a ``$n::jsonb`` parameter."""
    return json.dumps(value)


def artefact(r: Row) -> Artefact:
    """``artefacts`` row."""
    return Artefact(
        id=r["id"],
        tenant_id=r["tenant_id"],
        tender_id=r["tender_id"],
        kind=ArtefactKind(r["kind"]),
        capability=r["capability"],
        tender_section_ref=r["tender_section_ref"],
        target_ref=r["target_ref"],
        status=ArtefactStatus(r["status"]),
        current_version_id=r["current_version_id"],
        created_by=r["created_by"],
        archive_state=ArtefactArchiveState(r["archive_state"]),
        archived_at=r["archived_at"],
        record_version=r["record_version"],
        created_at=r["created_at"],
        updated_at=r["updated_at"],
    )


def artefact_version(r: Row) -> ArtefactVersion:
    """``artefact_versions`` row."""
    return ArtefactVersion(
        id=r["id"],
        artefact_id=r["artefact_id"],
        tenant_id=r["tenant_id"],
        version_no=r["version_no"],
        minted_reason=r["minted_reason"],
        origin=ArtefactOrigin(r["origin"]),
        evidence_tier=EvidenceTier(r["evidence_tier"]),
        completeness=CompletenessVerdict(r["completeness"]),
        superseded_by_section_edit=r["superseded_by_section_edit"],
        eligibility_state=EligibilityState(r["eligibility_state"]),
        requested_mode=GenerationMode(r["requested_mode"]),
        effective_mode=GenerationMode(r["effective_mode"]),
        clamped=r["clamped"],
        clamp_reason=r["clamp_reason"],
        domain_profile_version=r["domain_profile_version"],
        language_pack_version=r["language_pack_version"],
        model_profile_id=r["model_profile_id"],
        prompt_hash=r["prompt_hash"],
        parser_version=r["parser_version"],
        chunker_version=r["chunker_version"],
        generation_run_id=r["generation_run_id"],
        output_schema_version=r["output_schema_version"],
        delivery=DeliveryState(r["delivery"]),
        degradation_markers=load_json(r["degradation_markers"]),
        instruction=r["instruction"],
        instruction_refinement_id=r["instruction_refinement_id"],
        review_context_read=r["review_context_read"],
        review_context_ref=r["review_context_ref"],
        disclaimer_ref=r["disclaimer_ref"],
        created_at=r["created_at"],
    )


def section(r: Row) -> ArtefactSection:
    """``artefact_sections`` row."""
    failure = r["failure_reason"]
    return ArtefactSection(
        id=r["id"],
        artefact_id=r["artefact_id"],
        tenant_id=r["tenant_id"],
        tender_section_ref=r["tender_section_ref"],
        section_type=r["section_type"],
        ordinal=r["ordinal"],
        is_foundation=r["is_foundation"],
        state=SectionState(r["state"]),
        gap_reason=r["gap_reason"],
        failure_reason=None if failure is None else RunFailureReason(failure),
        current_version_id=r["current_version_id"],
        flagged_for_review=r["flagged_for_review"],
        flagged_reason=r["flagged_reason"],
        record_version=r["record_version"],
        created_at=r["created_at"],
        updated_at=r["updated_at"],
    )


def section_version(r: Row) -> SectionVersion:
    """``artefact_section_versions`` row."""
    return SectionVersion(
        id=r["id"],
        section_id=r["section_id"],
        tenant_id=r["tenant_id"],
        version_no=r["version_no"],
        origin=ArtefactOrigin(r["origin"]),
        content_ref=r["content_ref"],
        content_sha256=bytes(r["content_sha256"]),
        retrieval_set_id=r["retrieval_set_id"],
        evidence_tier=EvidenceTier(r["evidence_tier"]),
        completeness=CompletenessVerdict(r["completeness"]),
        scope_narrowed=r["scope_narrowed"],
        model_profile_id=r["model_profile_id"],
        prompt_hash=r["prompt_hash"],
        serving_tier=r["serving_tier"],
        auto_retry_count=r["auto_retry_count"],
        manual_regen_count=r["manual_regen_count"],
        degradation_markers=load_json(r["degradation_markers"]),
        created_at=r["created_at"],
    )


def groundedness(r: Row) -> GroundednessReport:
    """``groundedness_reports`` row."""
    return GroundednessReport(
        id=r["id"],
        section_version_id=r["section_version_id"],
        tenant_id=r["tenant_id"],
        tiers_run=tuple(r["tiers_run"]),
        claims_total=r["claims_total"],
        supported=r["supported"],
        unsupported=r["unsupported"],
        miscited=r["miscited"],
        gaps=r["gaps"],
        score=r["score"],
        evidence_tier=EvidenceTier(r["evidence_tier"]),
        judge_model_profile_id=r["judge_model_profile_id"],
        created_at=r["created_at"],
    )


def claim(r: Row) -> ClaimVerification:
    """``claim_verifications`` row."""
    corrected = r["corrected_chunk_ids"]
    return ClaimVerification(
        report_id=r["report_id"],
        claim_ordinal=r["claim_ordinal"],
        tenant_id=r["tenant_id"],
        outcome=ClaimOutcome(r["outcome"]),
        cited_chunk_ids=tuple(r["cited_chunk_ids"]),
        corrected_chunk_ids=None if corrected is None else tuple(corrected),
        tier_decided=r["tier_decided"],
    )


def completeness(r: Row) -> CompletenessCheck:
    """``completeness_checks`` row."""
    return CompletenessCheck(
        id=r["id"],
        section_version_id=r["section_version_id"],
        tenant_id=r["tenant_id"],
        verdict=CompletenessVerdict(r["verdict"]),
        items_total=r["items_total"],
        items_addressed=r["items_addressed"],
        missed_items=tuple(r["missed_items"]),
        created_at=r["created_at"],
    )


def retrieval_set(r: Row) -> RetrievalSet:
    """``retrieval_sets`` row."""
    return RetrievalSet(
        id=r["id"],
        tenant_id=r["tenant_id"],
        tender_id=r["tender_id"],
        artefact_id=r["artefact_id"],
        verdict=RetrievalVerdict(r["verdict"]),
        verdict_reason=r["verdict_reason"],
        scope_resolution_hash=r["scope_resolution_hash"],
        grant_level_applied=GrantLevel(r["grant_level_applied"]),
        grant_level_clamped=r["grant_level_clamped"],
        filter_set=load_json(r["filter_set"]),
        index_generations=load_json(r["index_generations"]),
        profile_source=r["profile_source"],
        applied_profile=load_json(r["applied_profile"]),
        values_provisional=r["values_provisional"],
        reranker_ran=r["reranker_ran"],
        budget_allocation=load_json(r["budget_allocation"]),
        query_refinement_id=r["query_refinement_id"],
        widened_retry_of=r["widened_retry_of"],
        domain_profile_version=r["domain_profile_version"],
        language_pack_version=r["language_pack_version"],
        degradation_markers=load_json(r["degradation_markers"]),
        created_at=r["created_at"],
    )


def retrieval_chunk(r: Row) -> RetrievalChunk:
    """``retrieval_set_chunks`` row."""
    span = r["source_span"]
    return RetrievalChunk(
        retrieval_set_id=r["retrieval_set_id"],
        tenant_id=r["tenant_id"],
        rank=r["rank"],
        chunk_id=r["chunk_id"],
        document_id=r["document_id"],
        document_version=r["document_version"],
        collection_id=r["collection_id"],
        collection_weight=r["collection_weight"],
        fused_score=r["fused_score"],
        rerank_score=r["rerank_score"],
        source_half=r["source_half"],
        parent_chunk_id=r["parent_chunk_id"],
        document_title=r["document_title"],
        heading_path=r["heading_path"],
        clause_ref=r["clause_ref"],
        document_role=r["document_role"],
        language=r["language"],
        excerpt=r["excerpt"],
        excerpt_is_translation=r["excerpt_is_translation"],
        source_span=None if span is None else load_json(span),
    )


def retrieval_purge(r: Row) -> RetrievalSetPurge:
    """``retrieval_set_purges`` row."""
    return RetrievalSetPurge(
        id=r["id"],
        retrieval_set_id=r["retrieval_set_id"],
        tenant_id=r["tenant_id"],
        purged_document_id=r["purged_document_id"],
        purged_version=r["purged_version"],
        chunk_ids=tuple(r["chunk_ids"]),
        event_id=r["event_id"],
        purged_at=r["purged_at"],
    )


def audit_entry(r: Row) -> AuditEntry:
    """``artefact_audit_chain`` row."""
    draft = AuditEntryDraft(
        tenant_id=r["tenant_id"],
        occurred_at=r["occurred_at"],
        entry_type=r["entry_type"],
        action=r["action"],
        actor_type=AuditActorType(r["actor_type"]),
        actor_id=r["actor_id"],
        artefact_id=r["artefact_id"],
        target_type=r["target_type"],
        target_id=r["target_id"],
        source_service=r["source_service"],
        source_event_type=r["source_event_type"],
        source_event_id=r["source_event_id"],
        trace_id=r["trace_id"],
        metadata=load_json(r["metadata"]),
    )
    return AuditEntry(
        draft=draft,
        seq=r["seq"],
        payload_hash=bytes(r["payload_hash"]),
        prev_hash=None if r["prev_hash"] is None else bytes(r["prev_hash"]),
        entry_hash=bytes(r["entry_hash"]),
    )
