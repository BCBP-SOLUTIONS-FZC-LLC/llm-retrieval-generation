"""W-2 ``GET /api/v1/generations/{id}``: run state served from this service's tables only.

``generation_runs``, ``artefact_sections`` and ``document_facts``: never a Temporal query
(ASYNC-5), so the route answers while the workers are slow or the engine is down. Scope is
re-resolved on every read at ``answer`` (API-10); a caller outside it gets ``404``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from uuid import UUID

from rag.application.shared.ports import TransactionRunner
from rag.application.shared.scope import ScopeAuthorizer, ScopeTarget
from rag.application.workflows.common import not_found
from rag.domain.shared.enums import GrantLevel, SectionState
from rag.domain.shared.errors import DomainError, ErrorCode
from rag.domain.shared.identity import RequestIdentity
from rag.domain.workflows.repositories import RunRepository
from rag.domain.workflows.views import RunStateView, SectionProgress


@dataclass(frozen=True, slots=True)
class OutlineSectionDTO:
    """One outline entry: the section row's reference and type (RUN-4)."""

    ref: str
    type: str


@dataclass(frozen=True, slots=True)
class OutlineDTO:
    """The outline's state and version (columns of the run)."""

    state: str
    version: int | None
    sections: list[OutlineSectionDTO]
    ready_at: datetime | None
    gate_expires_at: datetime | None


@dataclass(frozen=True, slots=True)
class SectionProgressDTO:
    """A section with the reason for its state: ``gap_reason`` or ``failure_reason``, never both."""

    ref: str
    state: str
    section_id: UUID
    section_version_id: UUID | None = None
    evidence_tier: str | None = None
    retrieval_set_id: UUID | None = None
    gap_reason: str | None = None
    failure_reason: str | None = None
    flagged_for_review: bool = False


@dataclass(frozen=True, slots=True)
class CoherenceDTO:
    """The coherence verdict (``not_computed`` until the pass runs, RUN-6)."""

    verdict: str
    computed_at: datetime | None


@dataclass(frozen=True, slots=True)
class ProgressDTO:
    """A count over the section rows."""

    total: int
    complete: int
    gap: int
    failed: int


@dataclass(frozen=True, slots=True)
class GenerationStateDTO:
    """W-2's body."""

    generation_id: UUID
    artefact_id: UUID
    status: str
    outline: OutlineDTO
    sections: list[SectionProgressDTO]
    established_facts: dict[str, str]
    coherence: CoherenceDTO
    scope_narrowed: bool
    progress: ProgressDTO
    failure_reason: str | None = None
    failed_at_stage: str | None = None
    current_stage: str | None = None
    origin: str = "original"
    errors: list[str] = field(default_factory=list)


def _ref(p: SectionProgress) -> str:
    return p.section.tender_section_ref or str(p.section.id)


def state_dto(view: RunStateView) -> GenerationStateDTO:
    """The composite as W-2 serves it."""
    run = view.run
    sections = [
        SectionProgressDTO(
            ref=_ref(p),
            state=p.section.state.value,
            section_id=p.section.id,
            section_version_id=p.section.current_version_id,
            evidence_tier=None if p.evidence_tier is None else p.evidence_tier.value,
            retrieval_set_id=p.retrieval_set_id,
            gap_reason=p.section.gap_reason,
            failure_reason=None
            if p.section.failure_reason is None
            else p.section.failure_reason.value,
            flagged_for_review=p.section.flagged_for_review,
        )
        for p in view.sections
    ]

    def count(state: SectionState) -> int:
        return sum(1 for p in view.sections if p.section.state is state)

    return GenerationStateDTO(
        generation_id=run.id,
        artefact_id=run.artefact_id,
        status=run.status.value,
        outline=OutlineDTO(
            state=run.outline_state.value,
            version=run.outline_version,
            sections=[OutlineSectionDTO(_ref(p), p.section.section_type) for p in view.sections],
            ready_at=run.outline_ready_at,
            gate_expires_at=run.gate_expires_at,
        ),
        sections=sections,
        established_facts={f.fact_key: f.value for f in view.facts if f.superseded_at is None},
        coherence=CoherenceDTO(run.coherence.value, run.coherence_computed_at),
        scope_narrowed=view.scope_narrowed,
        progress=ProgressDTO(
            total=len(view.sections),
            complete=count(SectionState.COMPLETE),
            gap=count(SectionState.GAP),
            failed=count(SectionState.FAILED),
        ),
        failure_reason=None if run.failure_reason is None else run.failure_reason.value,
        failed_at_stage=None if run.failed_at_stage is None else run.failed_at_stage.value,
        current_stage=None if run.current_stage is None else run.current_stage.value,
        origin=run.origin.value,
    )


class GetGenerationState:
    """W-2."""

    def __init__(self, tx: TransactionRunner, runs: RunRepository, scope: ScopeAuthorizer) -> None:
        self._tx, self._runs, self._scope = tx, runs, scope

    async def execute(self, identity: RequestIdentity, run_id: UUID) -> GenerationStateDTO:
        """The run's state, or ``404 ARTEFACT_NOT_FOUND`` (absent or out of scope, API-10)."""
        view = await self._tx.run_in_tx(
            lambda tx: self._runs.read_state(tx, run_id), read_only=True
        )
        if view is None:
            raise not_found()
        try:
            await self._scope.require_level(
                identity, ScopeTarget(tender_id=view.run.tender_id), GrantLevel.ANSWER
            )
        except DomainError as exc:
            if exc.code is ErrorCode.COLLECTION_NOT_FOUND:
                raise not_found() from exc
            raise
        return state_dto(view)
