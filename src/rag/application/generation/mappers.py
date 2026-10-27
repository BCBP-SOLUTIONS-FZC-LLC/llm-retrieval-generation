"""Domain -> application read-model mapping."""

from __future__ import annotations

from rag.application.generation.dtos import (
    ArtefactDTO,
    ChunkDTO,
    CitationDTO,
    CompletenessDetailDTO,
    EligibilityDTO,
    GroundednessDTO,
    PurgeDTO,
    RetrievalSetDTO,
    RetrievalVerdictDTO,
    SectionStateDTO,
    VersionDetailDTO,
    VersionSectionDTO,
    VersionSummaryDTO,
)
from rag.domain.generation.artefact import ArtefactVersion
from rag.domain.generation.retrieval import RetrievalSetView
from rag.domain.generation.views import (
    ArtefactView,
    SectionStateView,
    VersionDetail,
    VersionSectionDetail,
)


def eligibility_dto(version: ArtefactVersion) -> EligibilityDTO:
    """The eligibility verdict a version carries (API-5, GEN-6)."""
    return EligibilityDTO(
        state=version.eligibility_state.value,
        requested_mode=version.requested_mode.value,
        effective_mode=version.effective_mode.value,
        clamped=version.clamped,
        clamp_reason=version.clamp_reason,
        profile_version=version.domain_profile_version,
    )


def version_summary_dto(version: ArtefactVersion) -> VersionSummaryDTO:
    """A version without verification records."""
    return VersionSummaryDTO(
        id=version.id,
        version_no=version.version_no,
        minted_reason=version.minted_reason,
        origin=version.origin.value,
        evidence_tier=version.evidence_tier.value,
        completeness=version.completeness.value,
        delivery=version.delivery.value,
        eligibility=eligibility_dto(version),
        superseded_by_section_edit=version.superseded_by_section_edit,
        created_at=version.created_at,
    )


def artefact_dto(view: ArtefactView) -> ArtefactDTO:
    """A-1. The tier travels with the version and withholds nothing (AV-4, AOB-6)."""
    a = view.artefact
    return ArtefactDTO(
        id=a.id,
        tender_id=a.tender_id,
        kind=a.kind.value,
        capability=a.capability,
        tender_section_ref=a.tender_section_ref,
        target_ref=a.target_ref,
        status=a.status.value,
        archive_state=a.archive_state.value,
        archived_at=a.archived_at,
        created_by=a.created_by,
        current_run_id=view.current_run_id,
        record_version=a.record_version,
        created_at=a.created_at,
        updated_at=a.updated_at,
        current_version=None
        if view.current_version is None
        else version_summary_dto(view.current_version),
    )


def _section_dto(detail: VersionSectionDetail) -> VersionSectionDTO:
    sv, g, c = detail.section_version, detail.groundedness, detail.completeness
    return VersionSectionDTO(
        section_ref=detail.section.tender_section_ref,
        section_version_id=sv.id,
        evidence_tier=sv.evidence_tier.value,
        completeness=sv.completeness.value,
        retrieval_set_id=sv.retrieval_set_id,
        groundedness=None
        if g is None
        else GroundednessDTO(
            supported=g.supported,
            unsupported=g.unsupported,
            miscited=g.miscited,
            gaps=g.gaps,
            tiers_run=list(g.tiers_run),
            claims=[
                CitationDTO(
                    claim_ordinal=k.claim_ordinal,
                    outcome=k.outcome.value,
                    cited_chunk_ids=list(k.cited_chunk_ids),
                    corrected_chunk_ids=None
                    if k.corrected_chunk_ids is None
                    else list(k.corrected_chunk_ids),
                    tier_decided=k.tier_decided,
                )
                for k in sorted(detail.claims, key=lambda k: k.claim_ordinal)
            ],
        ),
        completeness_detail=None
        if c is None
        else CompletenessDetailDTO(c.items_total, c.items_addressed, list(c.missed_items)),
    )


def version_detail_dto(detail: VersionDetail) -> VersionDetailDTO:
    """A-3: the stored roll-up (AV-3) beside the per-section records."""
    v = detail.version
    return VersionDetailDTO(
        id=v.id,
        version_no=v.version_no,
        origin=v.origin.value,
        generation_run_id=v.generation_run_id,
        evidence_tier=v.evidence_tier.value,
        completeness=v.completeness.value,
        sections=[_section_dto(s) for s in detail.sections],
        model_profile_id=v.model_profile_id,
        prompt_hash=v.prompt_hash,
        domain_profile_version=v.domain_profile_version,
        language_pack_version=v.language_pack_version,
        parser_version=v.parser_version,
        chunker_version=v.chunker_version,
        instruction=v.instruction,
        instruction_refinement_id=v.instruction_refinement_id,
        review_context_read=v.review_context_read,
        review_context_ref=v.review_context_ref,
        degradation_markers=list(v.degradation_markers),
        delivery=v.delivery.value,
        eligibility=eligibility_dto(v),
        superseded_by_section_edit=v.superseded_by_section_edit,
        disclaimer_ref=v.disclaimer_ref,
    )


def section_state_dto(view: SectionStateView) -> SectionStateDTO:
    """A-4: five states, with the reason for the state and never both reasons (AS-5)."""
    s = view.section
    return SectionStateDTO(
        section_id=s.id,
        section_ref=s.tender_section_ref,
        ordinal=s.ordinal,
        section_type=s.section_type,
        is_foundation=s.is_foundation,
        state=s.state.value,
        gap_reason=s.gap_reason,
        failure_reason=None if s.failure_reason is None else s.failure_reason.value,
        section_version_id=s.current_version_id,
        evidence_tier=None if view.evidence_tier is None else view.evidence_tier.value,
        flagged_for_review=s.flagged_for_review,
        flagged_reason=s.flagged_reason,
    )


def retrieval_set_dto(view: RetrievalSetView) -> RetrievalSetDTO:
    """R-5: chunks intact with ``purged`` flags, purge rows beside them (RS-1, RSP-2)."""
    rs = view.retrieval_set
    purged = view.purged_chunk_ids
    return RetrievalSetDTO(
        id=rs.id,
        artefact_id=rs.artefact_id,
        tender_id=rs.tender_id,
        verdict=RetrievalVerdictDTO(
            retrieval_set_id=rs.id,
            verdict=rs.verdict.value,
            verdict_reason=rs.verdict_reason,
            widened_retry_of=rs.widened_retry_of,
            values_provisional=rs.values_provisional,
            degradation_markers=list(rs.degradation_markers),
        ),
        scope_resolution_hash=rs.scope_resolution_hash,
        grant_level_applied=rs.grant_level_applied.value,
        grant_level_clamped=rs.grant_level_clamped,
        filter_set=rs.filter_set,
        index_generations=rs.index_generations,
        applied_profile=rs.applied_profile,
        profile_source=rs.profile_source,
        budget_allocation=rs.budget_allocation,
        reranker_ran=rs.reranker_ran,
        domain_profile_version=rs.domain_profile_version,
        language_pack_version=rs.language_pack_version,
        query_refinement_id=rs.query_refinement_id,
        chunks=[
            ChunkDTO(
                rank=c.rank,
                chunk_id=c.chunk_id,
                parent_chunk_id=c.parent_chunk_id,
                document_id=c.document_id,
                document_version=c.document_version,
                document_title=c.document_title,
                heading_path=c.heading_path,
                clause_ref=c.clause_ref,
                document_role=c.document_role,
                language=c.language,
                collection_id=c.collection_id,
                collection_weight=float(c.collection_weight),
                fused_score=float(c.fused_score),
                rerank_score=None if c.rerank_score is None else float(c.rerank_score),
                source_half=c.source_half,
                excerpt=c.excerpt,
                excerpt_is_translation=c.excerpt_is_translation,
                source_span=c.source_span,
                purged=c.chunk_id in purged,
            )
            for c in sorted(view.chunks, key=lambda c: c.rank)
        ],
        purges=[
            PurgeDTO(p.purged_document_id, p.purged_version, list(p.chunk_ids), p.purged_at)
            for p in sorted(view.purges, key=lambda p: (p.purged_at is None, p.purged_at))
        ],
    )
