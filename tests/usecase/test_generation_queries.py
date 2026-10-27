"""A-1..A-4, A-8, A-9, R-5 use cases over in-memory fakes."""

from __future__ import annotations

from uuid import uuid4

import pytest
from tests.support_generation import (
    TENANT,
    TENDER,
    FakeReadRepo,
    FakeRetrievalRepo,
    FakeScope,
    FakeStore,
    FakeTx,
    claim_for,
    denied,
    identity,
    make_artefact,
    make_retrieval_view,
    make_section,
    make_section_version,
    make_version,
    report_for,
)

from rag.application.generation.queries import (
    GetArtefact,
    GetRetrievalSet,
    GetSectionVersionContent,
    GetVersion,
    ListSections,
    ListVersions,
    RenderArtefact,
)
from rag.domain.generation.artefact import Artefact
from rag.domain.shared.enums import (
    ArtefactKind,
    ClaimOutcome,
    EvidenceTier,
    GrantLevel,
    SectionState,
)
from rag.domain.shared.errors import DomainError, ErrorCode


class World:
    """One artefact with three versions, two sections and one verified section version."""

    def __init__(self, content: bytes = b"The access route ... [c-07]") -> None:
        self.repo = FakeReadRepo()
        self.tx = FakeTx()
        self.scope = FakeScope()
        self.art: Artefact = make_artefact()
        self.rs = make_retrieval_view()
        self.sec1 = make_section(self.art.id, 1)
        self.sec2 = make_section(self.art.id, 2)
        self.sv = make_section_version(
            self.sec1.id, self.rs.retrieval_set.id, content, evidence_tier=EvidenceTier.SUPPORTED
        )
        self.sec1.state = SectionState.COMPLETE
        self.sec1.current_version_id = self.sv.id
        self.versions = [make_version(self.art.id, n) for n in (1, 2, 3)]
        self.art.current_version_id = self.versions[2].id
        self.repo.artefacts[self.art.id] = self.art
        self.repo.versions = self.versions
        self.repo.sections = [self.sec1, self.sec2]
        self.repo.section_versions = [self.sv]
        self.repo.members[self.versions[2].id] = [self.sv.id]
        self.report = report_for(self.sv.id)
        self.repo.groundedness[self.sv.id] = self.report
        self.repo.claims = [
            claim_for(self.report.id, 2, ClaimOutcome.MISCITED),
            claim_for(self.report.id, 1),
        ]
        self.store = FakeStore({self.sv.content_ref: content})


@pytest.fixture
def w() -> World:
    return World()


async def test_a1_returns_artefact_current_version_and_authorizes_at_answer(w: World) -> None:
    dto = await GetArtefact(w.tx, w.repo, w.scope).execute(identity(), w.art.id)
    assert dto.id == w.art.id
    assert dto.current_version and dto.current_version.version_no == 3
    assert dto.current_run_id == w.repo.run_id
    assert dto.current_version.eligibility.profile_version == "dp-1"
    [(_, target, level)] = w.scope.calls
    assert (target.tender_id, level) == (TENDER, GrantLevel.ANSWER)  # API-10
    assert all(w.tx.calls)  # reads only


async def test_a1_low_tier_never_withholds_the_version(w: World) -> None:
    """AV-4 / AOB-6: a NOT_COMPUTED version is still returned in full."""
    w.repo.versions[2] = make_version(
        w.art.id, 3, id=w.art.current_version_id, evidence_tier=EvidenceTier.NOT_COMPUTED
    )
    dto = await GetArtefact(w.tx, w.repo, w.scope).execute(identity(), w.art.id)
    assert dto.current_version and dto.current_version.evidence_tier == "NOT_COMPUTED"


async def test_unknown_artefact_is_404_and_scope_is_not_consulted(w: World) -> None:
    with pytest.raises(DomainError) as err:
        await GetArtefact(w.tx, w.repo, w.scope).execute(identity(), uuid4())
    assert err.value.code is ErrorCode.ARTEFACT_NOT_FOUND
    assert w.scope.calls == []


async def test_revoked_grant_refuses_every_artefact_read(w: World) -> None:
    """API-10: an id authorizes nothing; the scope refusal propagates, nothing is read after."""
    w.scope.error = denied()
    for call in (
        GetArtefact(w.tx, w.repo, w.scope).execute(identity(), w.art.id),
        ListVersions(w.tx, w.repo, w.scope).execute(identity(), w.art.id),
        GetVersion(w.tx, w.repo, w.scope).execute(identity(), w.art.id, 3),
        ListSections(w.tx, w.repo, w.scope).execute(identity(), w.art.id),
        RenderArtefact(w.tx, w.repo, w.scope).execute(identity(), w.art.id),
        GetSectionVersionContent(w.tx, w.repo, w.scope, w.store).execute(
            identity(), w.art.id, "sec-1", 1
        ),
    ):
        with pytest.raises(DomainError) as err:
            await call
        assert err.value.code is ErrorCode.GRANT_LEVEL_INSUFFICIENT


async def test_scope_store_outage_fails_closed(w: World) -> None:
    w.scope.error = DomainError(ErrorCode.SCOPE_STORE_UNAVAILABLE)
    with pytest.raises(DomainError) as err:
        await GetArtefact(w.tx, w.repo, w.scope).execute(identity(), w.art.id)
    assert err.value.code is ErrorCode.SCOPE_STORE_UNAVAILABLE


async def test_a2_pagination_default_clamp_and_echo(w: World) -> None:
    uc = ListVersions(w.tx, w.repo, w.scope)
    page = await uc.execute(identity(), w.art.id)
    assert (page.limit, page.offset, page.total) == (100, 0, 3)
    assert [v.version_no for v in page.items] == [3, 2, 1]
    clamped = await uc.execute(identity(), w.art.id, limit=10_000)
    assert clamped.limit == 500
    second = await uc.execute(identity(), w.art.id, limit=1, offset=1)
    assert [v.version_no for v in second.items] == [2]
    assert (second.limit, second.offset, second.total) == (1, 1, 3)


async def test_a3_version_carries_per_section_reports_and_retrieval_set_ref(w: World) -> None:
    dto = await GetVersion(w.tx, w.repo, w.scope).execute(identity(), w.art.id, 3)
    assert dto.version_no == 3 and len(dto.sections) == 1
    s = dto.sections[0]
    assert s.retrieval_set_id == w.rs.retrieval_set.id  # API-9
    assert s.section_ref == "sec-1"
    assert s.groundedness and (s.groundedness.supported, s.groundedness.miscited) == (11, 1)
    assert [c.claim_ordinal for c in s.groundedness.claims] == [1, 2]
    assert s.groundedness.claims[1].corrected_chunk_ids == ["dv2:2"]
    assert s.completeness_detail is None  # none recorded


async def test_a3_unknown_version_is_404(w: World) -> None:
    with pytest.raises(DomainError) as err:
        await GetVersion(w.tx, w.repo, w.scope).execute(identity(), w.art.id, 99)
    assert err.value.code is ErrorCode.ARTEFACT_NOT_FOUND


async def test_a4_sections_show_state_reason_and_tier(w: World) -> None:
    w.sec2.state = SectionState.GAP
    w.sec2.gap_reason = "insufficient_twice"
    page = await ListSections(w.tx, w.repo, w.scope).execute(identity(), w.art.id)
    first, second = page.items
    assert (first.state, first.evidence_tier, first.section_version_id) == (
        "complete",
        "SUPPORTED",
        w.sv.id,
    )
    assert (second.state, second.gap_reason, second.failure_reason, second.evidence_tier) == (
        "gap",
        "insufficient_twice",
        None,
        None,
    )


async def test_a8_is_503_renderer_unavailable_after_scope_and_artefact(w: World) -> None:
    with pytest.raises(DomainError) as err:
        await RenderArtefact(w.tx, w.repo, w.scope).execute(identity(), w.art.id)
    assert err.value.code is ErrorCode.RENDERER_UNAVAILABLE
    assert len(w.scope.calls) == 1  # checked first, so a stranger learns nothing
    with pytest.raises(DomainError) as missing:
        await RenderArtefact(w.tx, w.repo, w.scope).execute(identity(), uuid4())
    assert missing.value.code is ErrorCode.ARTEFACT_NOT_FOUND


async def test_a9_reads_text_from_the_store(w: World) -> None:
    uc = GetSectionVersionContent(w.tx, w.repo, w.scope, w.store)
    dto = await uc.execute(identity(), w.art.id, "sec-1", 1)
    assert dto.content == "The access route ... [c-07]"
    assert (dto.content_type, dto.retrieval_set_id) == ("text/markdown", w.rs.retrieval_set.id)
    assert dto.evidence_tier == "SUPPORTED"


@pytest.mark.parametrize(("ref", "no"), [("sec-2", 1), ("sec-1", 9), ("nope", 1)])
async def test_a9_section_without_that_version_is_404(w: World, ref: str, no: int) -> None:
    """A gap/failed section has no version; an absent ref or number is the same 404."""
    uc = GetSectionVersionContent(w.tx, w.repo, w.scope, w.store)
    with pytest.raises(DomainError) as err:
        await uc.execute(identity(), w.art.id, ref, no)
    assert err.value.code is ErrorCode.ARTEFACT_NOT_FOUND


async def test_a9_unreadable_object_is_503(w: World) -> None:
    w.store.objects.clear()
    with pytest.raises(DomainError) as err:
        await GetSectionVersionContent(w.tx, w.repo, w.scope, w.store).execute(
            identity(), w.art.id, "sec-1", 1
        )
    assert err.value.code is ErrorCode.ARTEFACT_CONTENT_UNAVAILABLE


async def test_a9_altered_bytes_fail_the_asv4_digest_check(w: World) -> None:
    w.store.objects[w.sv.content_ref] = b"tampered"
    with pytest.raises(DomainError) as err:
        await GetSectionVersionContent(w.tx, w.repo, w.scope, w.store).execute(
            identity(), w.art.id, "sec-1", 1
        )
    assert err.value.code is ErrorCode.ARTEFACT_CONTENT_UNAVAILABLE


async def test_a9_free_standing_answer_resolves_with_a_tenant_wide_scope() -> None:
    """ART-1/AS-4: no tender, section addressed by id; scope target carries no tender."""
    w = World()
    answer = make_artefact(tender_id=None, kind=ArtefactKind.ANSWER)
    sec = make_section(answer.id, tender_section_ref=None)
    sv = make_section_version(sec.id, w.rs.retrieval_set.id, b"a")
    w.repo.artefacts[answer.id] = answer
    w.repo.sections.append(sec)
    w.repo.section_versions.append(sv)
    w.store.objects[sv.content_ref] = b"a"
    dto = await GetSectionVersionContent(w.tx, w.repo, w.scope, w.store).execute(
        identity(), answer.id, str(sec.id), 1
    )
    assert dto.content == "a"
    assert w.scope.calls[-1][1].tender_id is None


# --- R-5 -----------------------------------------------------------------------------------


async def test_r5_returns_set_with_chunks_and_search_level_scope() -> None:
    view = make_retrieval_view()
    scope, tx = FakeScope(), FakeTx()
    dto = await GetRetrievalSet(tx, FakeRetrievalRepo(view), scope).execute(
        identity(), view.retrieval_set.id
    )
    assert [c.rank for c in dto.chunks] == [1, 2] and not any(c.purged for c in dto.chunks)
    assert dto.verdict.verdict == "sufficient" and dto.purges == []
    [(_, target, level)] = scope.calls
    assert level is GrantLevel.SEARCH
    assert set(target.collection_ids) == {c.collection_id for c in view.chunks}


async def test_r5_purge_markers_flag_chunks_and_leave_excerpts_intact() -> None:
    """RS-1 / RSP-2: purged chunk keeps its excerpt; the marker rides beside the set."""
    from tests.support_generation import FakeRetrievalRepo as Repo

    view = make_retrieval_view()
    repo = Repo(view)
    c1 = view.chunks[0]
    event = uuid4()
    n = await repo.purge_for_document(
        None,
        tenant_id=TENANT,
        collection_id=c1.collection_id,
        document_id=c1.document_id,
        document_version=c1.document_version,
        event_id=event,
    )
    assert n == 1
    dto = await GetRetrievalSet(FakeTx(), repo, FakeScope()).execute(
        identity(), view.retrieval_set.id
    )
    flags = {c.chunk_id: (c.purged, c.excerpt) for c in dto.chunks}
    assert flags == {"dv1:1": (True, "excerpt 1"), "dv2:2": (False, "excerpt 2")}
    assert [(p.purged_document_id, p.chunk_ids) for p in dto.purges] == [
        (c1.document_id, ["dv1:1"])
    ]
    assert dto.verdict.verdict == "sufficient"  # the verdict is never edited


async def test_r5_revoked_grant_and_absent_set_are_the_same_404() -> None:
    view = make_retrieval_view()
    repo, tx = FakeRetrievalRepo(view), FakeTx()
    with pytest.raises(DomainError) as absent:
        await GetRetrievalSet(tx, repo, FakeScope()).execute(identity(), uuid4())
    with pytest.raises(DomainError) as revoked:
        await GetRetrievalSet(tx, repo, FakeScope(denied())).execute(
            identity(), view.retrieval_set.id
        )
    assert absent.value.code is revoked.value.code is ErrorCode.RETRIEVAL_SET_NOT_FOUND


async def test_r5_scope_outage_stays_503() -> None:
    view = make_retrieval_view()
    scope = FakeScope(DomainError(ErrorCode.SCOPE_STORE_UNAVAILABLE))
    with pytest.raises(DomainError) as err:
        await GetRetrievalSet(FakeTx(), FakeRetrievalRepo(view), scope).execute(
            identity(), view.retrieval_set.id
        )
    assert err.value.code is ErrorCode.SCOPE_STORE_UNAVAILABLE
