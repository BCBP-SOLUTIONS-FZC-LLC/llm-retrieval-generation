"""Builders and in-memory fakes for the generation-record slice's tests."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Sequence
from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

from rag.application.shared.scope import ScopeTarget
from rag.domain.generation import audit
from rag.domain.generation.artefact import Artefact, ArtefactVersion
from rag.domain.generation.audit import AuditEntry, AuditEntryDraft
from rag.domain.generation.retrieval import (
    RetrievalChunk,
    RetrievalSet,
    RetrievalSetPurge,
    RetrievalSetView,
)
from rag.domain.generation.section import ArtefactSection, SectionVersion
from rag.domain.generation.verification import (
    ClaimVerification,
    CompletenessCheck,
    GroundednessReport,
)
from rag.domain.generation.views import (
    ArtefactView,
    SectionStateView,
    SectionVersionRecord,
    VersionDetail,
    VersionSectionDetail,
)
from rag.domain.shared.enums import (
    ArtefactKind,
    ClaimOutcome,
    CompletenessVerdict,
    DeliveryState,
    EligibilityState,
    EvidenceTier,
    GenerationMode,
    GrantLevel,
    RetrievalVerdict,
    SectionState,
)
from rag.domain.shared.errors import DomainError, ErrorCode
from rag.domain.shared.identity import RequestIdentity

TENANT = UUID("0a3f0000-0000-7000-8000-000000000001")
USER = UUID("0a3f0000-0000-7000-8000-000000000002")
TENDER = UUID("0a3f0000-0000-7000-8000-0000000000a1")
NOW = datetime(2026, 4, 2, 9, 0, tzinfo=UTC)


def identity(tenant: UUID = TENANT) -> RequestIdentity:
    """A caller of ``tenant``."""
    return RequestIdentity(tenant_id=tenant, principal_id=USER)


def make_artefact(**kw: Any) -> Artefact:
    """A live document artefact of ``TENDER``."""
    base: dict[str, Any] = dict(
        id=uuid4(),
        tenant_id=TENANT,
        tender_id=TENDER,
        kind=ArtefactKind.DOCUMENT,
        capability="technical_proposal",
        created_by=USER,
        created_at=NOW,
        updated_at=NOW,
    )
    return Artefact(**{**base, **kw})


def make_version(artefact_id: UUID, version_no: int = 1, **kw: Any) -> ArtefactVersion:
    """A minted version."""
    base: dict[str, Any] = dict(
        id=uuid4(),
        artefact_id=artefact_id,
        tenant_id=TENANT,
        version_no=version_no,
        minted_reason="completion",
        evidence_tier=EvidenceTier.PARTIAL,
        completeness=CompletenessVerdict.PARTIAL,
        eligibility_state=EligibilityState.GENERATABLE,
        requested_mode=GenerationMode.ASSISTANCE,
        effective_mode=GenerationMode.ASSISTANCE,
        domain_profile_version="dp-1",
        language_pack_version="lp-1",
        model_profile_id="gen-large",
        prompt_hash="sha256:aa",
        parser_version="p-1",
        chunker_version="c-1",
        generation_run_id=uuid4(),
        delivery=DeliveryState.DELIVERED,
        created_at=NOW,
    )
    return ArtefactVersion(**{**base, **kw})


def make_section(artefact_id: UUID, ordinal: int = 1, **kw: Any) -> ArtefactSection:
    """A section."""
    base: dict[str, Any] = dict(
        id=uuid4(),
        artefact_id=artefact_id,
        tenant_id=TENANT,
        section_type="introduction",
        ordinal=ordinal,
        tender_section_ref=f"sec-{ordinal}",
    )
    return ArtefactSection(**{**base, **kw})


def make_section_version(
    section_id: UUID, retrieval_set_id: UUID, content: bytes = b"hello", **kw: Any
) -> SectionVersion:
    """A section version whose ``content_sha256`` matches ``content``."""
    import hashlib

    base: dict[str, Any] = dict(
        id=uuid4(),
        section_id=section_id,
        tenant_id=TENANT,
        version_no=1,
        content_ref=f"tenants/{TENANT}/artefacts/x/sections/{section_id}/v1.md",
        content_sha256=hashlib.sha256(content).digest(),
        retrieval_set_id=retrieval_set_id,
        evidence_tier=EvidenceTier.PARTIAL,
        completeness=CompletenessVerdict.PARTIAL,
        model_profile_id="gen-large",
        prompt_hash="sha256:aa",
        serving_tier="primary",
    )
    return SectionVersion(**{**base, **kw})


def make_retrieval_view(collection: UUID | None = None, **kw: Any) -> RetrievalSetView:
    """A retrieval set of two chunks (documents d1 and d2) and no purges."""
    set_id = uuid4()
    coll = collection or uuid4()
    rs = RetrievalSet(
        id=set_id,
        tenant_id=TENANT,
        tender_id=kw.pop("tender_id", TENDER),
        verdict=RetrievalVerdict.SUFFICIENT,
        scope_resolution_hash="sha256:1c",
        grant_level_applied=GrantLevel.ANSWER,
        filter_set={"collection_ids": [str(coll)]},
        index_generations={str(coll): 3},
        profile_source="section_type",
        applied_profile={},
        reranker_ran=True,
        budget_allocation={"window": 1000},
        domain_profile_version="dp-1",
        language_pack_version="lp-1",
        created_at=NOW,
        **kw,
    )
    chunks = tuple(
        RetrievalChunk(
            retrieval_set_id=set_id,
            tenant_id=TENANT,
            rank=i,
            chunk_id=f"dv{i}:{i}",
            document_id=uuid4(),
            document_version=1,
            collection_id=coll,
            collection_weight=Decimal("0.90"),
            fused_score=Decimal("0.03"),
            source_half="both",
            document_title=f"Doc {i}",
            document_role="specification",
            language="en",
            excerpt=f"excerpt {i}",
        )
        for i in (1, 2)
    )
    return RetrievalSetView(rs, chunks, ())


class FakeTx:
    """``TransactionRunner`` that hands the callback ``None`` as the connection."""

    def __init__(self) -> None:
        self.calls: list[bool] = []

    async def run_in_tx[T](
        self, fn: Callable[[Any], Awaitable[T]], *, read_only: bool = False
    ) -> T:
        self.calls.append(read_only)
        return await fn(None)


class FakeScope:
    """``ScopeAuthorizer`` recording each call; ``error`` makes it refuse."""

    def __init__(self, error: DomainError | None = None) -> None:
        self.error = error
        self.calls: list[tuple[RequestIdentity, ScopeTarget, GrantLevel]] = []

    async def require_level(
        self, identity: RequestIdentity, target: ScopeTarget, level: GrantLevel
    ) -> None:
        self.calls.append((identity, target, level))
        if self.error is not None:
            raise self.error


def denied() -> DomainError:
    """A revoked-grant refusal."""
    return DomainError(ErrorCode.GRANT_LEVEL_INSUFFICIENT)


class FakeStore:
    """``ArtefactContentStore`` over a dict; ``fail_delete`` simulates an S3 outage."""

    def __init__(self, objects: dict[str, bytes] | None = None, fail_delete: bool = False) -> None:
        self.objects = objects or {}
        self.fail_delete = fail_delete
        self.deleted: list[tuple[UUID, UUID | None]] = []

    async def get(self, tenant_id: UUID, content_ref: str) -> bytes:
        if content_ref not in self.objects:
            raise DomainError(ErrorCode.ARTEFACT_CONTENT_UNAVAILABLE)
        return self.objects[content_ref]

    async def delete_prefix(self, tenant_id: UUID, artefact_id: UUID | None = None) -> int:
        if self.fail_delete:
            raise RuntimeError("s3 down")
        self.deleted.append((tenant_id, artefact_id))
        return 1


class FakeReadRepo:
    """In-memory ``ArtefactReadRepository`` over explicit lists."""

    def __init__(self) -> None:
        self.artefacts: dict[UUID, Artefact] = {}
        self.versions: list[ArtefactVersion] = []
        self.sections: list[ArtefactSection] = []
        self.section_versions: list[SectionVersion] = []
        self.groundedness: dict[UUID, GroundednessReport] = {}
        self.claims: list[ClaimVerification] = []
        self.completeness: dict[UUID, CompletenessCheck] = {}
        self.members: dict[UUID, list[UUID]] = {}  # version id -> section version ids
        self.run_id: UUID | None = uuid4()

    async def get_artefact(self, tx: Any, artefact_id: UUID) -> Artefact | None:
        return self.artefacts.get(artefact_id)

    async def get_artefact_view(self, tx: Any, artefact_id: UUID) -> ArtefactView | None:
        a = self.artefacts.get(artefact_id)
        if a is None:
            return None
        cur = next((v for v in self.versions if v.id == a.current_version_id), None)
        return ArtefactView(a, cur, self.run_id)

    async def list_versions(
        self, tx: Any, artefact_id: UUID, *, limit: int, offset: int
    ) -> tuple[list[ArtefactVersion], int]:
        mine = sorted(
            (v for v in self.versions if v.artefact_id == artefact_id),
            key=lambda v: -v.version_no,
        )
        return mine[offset : offset + limit], len(mine)

    async def get_version_detail(
        self, tx: Any, artefact_id: UUID, version_no: int
    ) -> VersionDetail | None:
        v = next(
            (
                v
                for v in self.versions
                if v.artefact_id == artefact_id and v.version_no == version_no
            ),
            None,
        )
        if v is None:
            return None
        details = []
        for sv_id in self.members.get(v.id, []):
            sv = next(s for s in self.section_versions if s.id == sv_id)
            sec = next(s for s in self.sections if s.id == sv.section_id)
            g = self.groundedness.get(sv_id)
            claims = tuple(c for c in self.claims if g and c.report_id == g.id)
            details.append(VersionSectionDetail(sec, sv, g, claims, self.completeness.get(sv_id)))
        return VersionDetail(v, tuple(details))

    async def list_sections(
        self, tx: Any, artefact_id: UUID, *, limit: int, offset: int
    ) -> tuple[list[SectionStateView], int]:
        mine = sorted(
            (s for s in self.sections if s.artefact_id == artefact_id), key=lambda s: s.ordinal
        )
        views = []
        for s in mine[offset : offset + limit]:
            sv = next((x for x in self.section_versions if x.id == s.current_version_id), None)
            views.append(SectionStateView(s, None if sv is None else sv.evidence_tier))
        return views, len(mine)

    async def get_section_version_record(
        self, tx: Any, artefact_id: UUID, section_ref: str, version_no: int
    ) -> SectionVersionRecord | None:
        sec = next(
            (
                s
                for s in self.sections
                if s.artefact_id == artefact_id and section_ref in (s.tender_section_ref, str(s.id))
            ),
            None,
        )
        if sec is None:
            return None
        sv = next(
            (
                x
                for x in self.section_versions
                if x.section_id == sec.id and x.version_no == version_no
            ),
            None,
        )
        return None if sv is None else SectionVersionRecord(sec, sv)


class FakeRetrievalRepo:
    """In-memory ``RetrievalSetRepository`` with ``uq_purge_event`` semantics."""

    def __init__(self, *views: RetrievalSetView) -> None:
        self.views = {v.retrieval_set.id: v for v in views}

    async def get_view(self, tx: Any, retrieval_set_id: UUID) -> RetrievalSetView | None:
        return self.views.get(retrieval_set_id)

    async def purge_for_document(
        self,
        tx: Any,
        *,
        tenant_id: UUID,
        collection_id: UUID,
        document_id: UUID,
        document_version: int,
        event_id: UUID,
    ) -> int:
        inserted = 0
        for sid, view in list(self.views.items()):
            hits = tuple(
                c.chunk_id
                for c in view.chunks
                if (c.tenant_id, c.collection_id, c.document_id, c.document_version)
                == (tenant_id, collection_id, document_id, document_version)
            )
            if not hits or any(p.event_id == event_id for p in view.purges):
                continue
            purge = RetrievalSetPurge(
                uuid4(), sid, tenant_id, document_id, document_version, hits, event_id, NOW
            )
            self.views[sid] = replace(view, purges=(*view.purges, purge))
            inserted += 1
        return inserted


class FakeAuditRepo:
    """In-memory chain computing seq and hashes like ``audit_chain_append()``."""

    def __init__(self) -> None:
        self.entries: list[AuditEntry] = []

    async def append(self, tx: Any, draft: AuditEntryDraft) -> AuditEntry | None:
        mine = [e for e in self.entries if e.draft.tenant_id == draft.tenant_id]
        if any(e.draft.source_event_id == draft.source_event_id for e in mine):
            return None
        prev = mine[-1] if mine else None
        seq = len(mine) + 1
        payload = audit.compute_payload_hash(draft, seq)
        prev_hash = None if prev is None else prev.entry_hash
        entry = AuditEntry(
            draft, seq, payload, prev_hash, audit.compute_entry_hash(prev_hash, payload)
        )
        self.entries.append(entry)
        return entry

    async def list_chain(self, tx: Any, tenant_id: UUID, *, after_seq: int = 0) -> list[AuditEntry]:
        return [e for e in self.entries if e.draft.tenant_id == tenant_id and e.seq > after_seq]


class FakePurgeRepo:
    """In-memory ``ArtefactPurgeRepository``: records what it was asked to delete."""

    def __init__(self, artefacts: Sequence[Artefact]) -> None:
        self.artefacts = list(artefacts)
        self.deleted: list[tuple[UUID, UUID | None, list[UUID]]] = []

    async def list_artefacts_for_purge(
        self, tx: Any, tenant_id: UUID, tender_id: UUID | None
    ) -> list[Artefact]:
        return [
            a
            for a in self.artefacts
            if a.tenant_id == tenant_id and (tender_id is None or a.tender_id == tender_id)
        ]

    async def delete_artefact_trees(
        self, tx: Any, tenant_id: UUID, tender_id: UUID | None, artefact_ids: Sequence[UUID]
    ) -> None:
        self.deleted.append((tenant_id, tender_id, list(artefact_ids)))
        self.artefacts = [a for a in self.artefacts if a.id not in set(artefact_ids)]


class FixedClock:
    """A constant clock."""

    def now(self) -> datetime:
        return NOW


def report_for(section_version_id: UUID, tenant: UUID = TENANT) -> GroundednessReport:
    """A report with 11 supported, 1 unsupported, 1 miscited, 0 gaps."""
    return GroundednessReport(
        id=uuid4(),
        section_version_id=section_version_id,
        tenant_id=tenant,
        tiers_run=(1, 2),
        claims_total=13,
        supported=11,
        unsupported=1,
        miscited=1,
        gaps=0,
        evidence_tier=EvidenceTier.PARTIAL,
        score=Decimal("0.846"),
    )


def claim_for(
    report_id: UUID, ordinal: int, outcome: ClaimOutcome = ClaimOutcome.SUPPORTED
) -> ClaimVerification:
    """One claim."""
    return ClaimVerification(
        report_id=report_id,
        claim_ordinal=ordinal,
        tenant_id=TENANT,
        outcome=outcome,
        cited_chunk_ids=("dv1:1",),
        corrected_chunk_ids=("dv2:2",) if outcome is ClaimOutcome.MISCITED else None,
        tier_decided=1,
    )


def section_in_state(artefact_id: UUID, state: SectionState, **kw: Any) -> ArtefactSection:
    """A section already in ``state`` with the reason column that state requires."""
    extra: dict[str, Any] = {}
    if state is SectionState.GAP:
        extra["gap_reason"] = "insufficient_twice"
    return make_section(artefact_id, state=state, **{**extra, **kw})
