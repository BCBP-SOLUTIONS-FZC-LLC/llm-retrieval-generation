"""A-5, A-6 and A-7 over fakes: regeneration contracts, the refiner and fact-conflict choices."""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, replace
from uuid import uuid4

import pytest
from tests.support_generate import drain
from tests.support_generation import make_artefact, make_section, make_section_version, make_version
from tests.support_workflows import (
    TENANT,
    TENDER,
    USER,
    WfRig,
    engine_down,
)

from rag.application.generation.frames import VerdictFrame
from rag.application.generation.regenerate import (
    RegenerateDocumentCommand,
    RegenerateSectionCommand,
)
from rag.application.shared.gateway import GatewayQuotaRefused, GatewayUnavailable
from rag.domain.generation import audit
from rag.domain.generation.artefact import Artefact, ArtefactVersion
from rag.domain.generation.facts import DocumentFact, FactConflict
from rag.domain.generation.section import ArtefactSection, SectionVersion
from rag.domain.packs.generation import EligibilityDecl
from rag.domain.shared.enums import (
    ArtefactKind,
    ArtefactOrigin,
    ArtefactStatus,
    ConflictResolution,
    EligibilityState,
    RefinerOutcome,
    RunFailureReason,
    RunStatus,
    SectionState,
)
from rag.domain.shared.errors import DomainError, ErrorCode
from rag.domain.shared.identity import READ_ONLY_FLAG, RequestIdentity

ID = RequestIdentity(tenant_id=TENANT, principal_id=USER)
OLD = b"Old section text about crane erection."


def revoked() -> DomainError:
    return DomainError(ErrorCode.COLLECTION_NOT_FOUND)


async def raises(coro, code: ErrorCode) -> DomainError:  # noqa: ANN001
    with pytest.raises(DomainError) as exc:
        await coro
    assert exc.value.code is code, exc.value
    return exc.value


@dataclass
class Doc:
    artefact: Artefact
    version: ArtefactVersion
    s1: ArtefactSection
    s2: ArtefactSection
    sv1: SectionVersion
    sv2: SectionVersion


def seed(r: WfRig, *, kind: ArtefactKind = ArtefactKind.DOCUMENT, facts: bool = True) -> Doc:
    """A completed artefact with two sections, one document version holding both, one fact."""
    art = make_artefact(kind=kind, status=ArtefactStatus.COMPLETE)
    version = make_version(art.id, 1)
    art = replace(art, current_version_id=version.id)
    sections, versions = [], []
    for i, kind_ in enumerate(("method_statement", "introduction"), start=1):
        s = make_section(art.id, ordinal=i, section_type=kind_)
        sv = make_section_version(s.id, uuid4(), content=OLD, version_no=1)
        sv = replace(sv, content_ref=f"tenants/{TENANT}/artefacts/{art.id}/sections/{s.id}/v1.md")
        r.store.objects[sv.content_ref] = OLD
        s = replace(s, state=SectionState.COMPLETE, current_version_id=sv.id)
        r.world.sections[s.id], r.world.section_versions[sv.id] = s, sv
        r.world.holds.add((version.id, sv.id))
        sections.append(s)
        versions.append(sv)
    r.world.artefacts[art.id] = art
    r.world.versions[version.id] = version
    if facts:
        r.world.facts.append(
            DocumentFact(uuid4(), art.id, TENANT, "access_method", "crane", sections[0].id)
        )
    return Doc(art, version, sections[0], sections[1], versions[0], versions[1])


def a5(doc: Doc, **kw: object) -> RegenerateDocumentCommand:
    base: dict[str, object] = dict(
        artefact_id=doc.artefact.id, base_version_id=doc.version.id, instruction="Tighten it."
    )
    return RegenerateDocumentCommand(**{**base, **kw})  # type: ignore[arg-type]


def a6(doc: Doc, **kw: object) -> RegenerateSectionCommand:
    base: dict[str, object] = dict(
        artefact_id=doc.artefact.id,
        section_ref="sec-1",
        base_version_id=doc.sv1.id,
        instruction="Cite the night-work restriction.",
    )
    return RegenerateSectionCommand(**{**base, **kw})  # type: ignore[arg-type]


# =============================== A-5 ==============================================================
async def test_a5_opens_a_run_under_the_derived_workflow_id_and_starts_it() -> None:
    r = WfRig()
    doc = seed(r)
    handle = await r.regenerate_document.execute(ID, a5(doc))
    (run,) = r.world.runs.values()
    assert (run.origin, run.base_version_id) == (ArtefactOrigin.REGENERATION, doc.version.id)
    assert run.workflow_id == f"regen:{doc.version.id}"  # RUN-2, LD-55: not the artefact's id
    assert handle.generation_id == run.id and not handle.replayed
    (start,) = r.workflows.starts
    assert (start.workflow_id, start.workflow_type, start.restart) == (
        run.workflow_id,
        "GenerationWorkflow",
        False,
    )
    assert run.run_input is not None
    assert (
        run.run_input["origin"] == "regeneration" and run.run_input["instruction"] == "Tighten it."
    )
    assert run.run_input["base_version_id"] == str(doc.version.id)
    assert (
        r.world.artefacts[doc.artefact.id].status is ArtefactStatus.COMPLETE
    )  # the worker moves it
    assert r.world.types() == ["GenerationStarted"]
    assert r.world.events[0].payload["synchronous"] is False


async def test_a5_a_repeat_from_the_same_base_version_is_one_run() -> None:
    r = WfRig()
    doc = seed(r)
    first = await r.regenerate_document.execute(ID, a5(doc))
    run = r.world.runs[first.generation_id]
    r.world.runs[run.id] = replace(run, status=RunStatus.GENERATING)
    again = await r.regenerate_document.execute(ID, a5(doc))  # the artefact is not at rest now
    assert again.replayed and again.generation_id == first.generation_id
    assert len(r.world.runs) == 1 and len(r.workflows.starts) == 1  # absorbed, never a second run


async def test_a5_a_repeat_whose_first_start_failed_starts_it_now() -> None:
    r = WfRig()
    doc = seed(r)
    r.workflows.fail_start = engine_down()
    await raises(r.regenerate_document.execute(ID, a5(doc)), ErrorCode.WORKFLOW_ENGINE_UNAVAILABLE)
    (run,) = r.world.runs.values()
    assert run.status is RunStatus.PENDING  # recoverable: the row is committed
    r.workflows.fail_start = None
    again = await r.regenerate_document.execute(ID, a5(doc))
    assert again.replayed and len(r.world.runs) == 1 and len(r.workflows.starts) == 1


async def test_a5_a_lost_race_to_insert_resolves_to_the_winners_run() -> None:
    r = WfRig()
    doc = seed(r)
    winner = await r.regenerate_document.execute(ID, a5(doc))
    first_call = {"n": 0}
    real = r.runs.find_run_by_workflow_id

    async def blind_first(tx, workflow_id):  # noqa: ANN001, ANN202
        first_call["n"] += 1
        return None if first_call["n"] == 1 else await real(tx, workflow_id)

    r.regenerate_document._find = blind_first  # the pre-check misses; the locked re-check sees it
    again = await r.regenerate_document.execute(ID, a5(doc))
    assert again.generation_id == winner.generation_id and len(r.world.runs) == 1


async def test_a5_a_different_base_version_is_a_different_run() -> None:
    r = WfRig()
    doc = seed(r)
    await r.regenerate_document.execute(ID, a5(doc))
    r.world.artefacts[doc.artefact.id] = replace(r.world.artefacts[doc.artefact.id])
    v2 = make_version(doc.artefact.id, 2)
    r.world.versions[v2.id] = v2
    r.world.artefacts[doc.artefact.id] = replace(
        r.world.artefacts[doc.artefact.id], current_version_id=v2.id
    )
    second = await r.regenerate_document.execute(ID, a5(doc, base_version_id=v2.id))
    assert not second.replayed and len(r.world.runs) == 2


async def test_a5_refusals_and_the_stale_guard() -> None:
    r = WfRig()
    doc = seed(r)
    await raises(
        r.regenerate_document.execute(ID, a5(doc, base_version_id=uuid4())),
        ErrorCode.OPTIMISTIC_LOCK_CONFLICT,  # REGEN-2
    )
    await raises(
        r.regenerate_document.execute(ID, a5(doc, artefact_id=uuid4())),
        ErrorCode.ARTEFACT_NOT_FOUND,
    )
    r.world.artefacts[doc.artefact.id] = replace(
        r.world.artefacts[doc.artefact.id], status=ArtefactStatus.GENERATING
    )
    await raises(r.regenerate_document.execute(ID, a5(doc)), ErrorCode.OPTIMISTIC_LOCK_CONFLICT)
    assert r.world.runs == {} and r.workflows.starts == []


async def test_a5_a_closed_tender_is_409_whether_archived_or_gated() -> None:
    r = WfRig()
    doc = seed(r)
    r.gate.closed.add(TENDER)
    await raises(r.regenerate_document.execute(ID, a5(doc)), ErrorCode.TENDER_CLOSED)  # COL-14
    r.gate.closed.clear()
    from datetime import UTC, datetime

    from rag.domain.shared.enums import ArtefactArchiveState

    r.world.artefacts[doc.artefact.id] = replace(
        r.world.artefacts[doc.artefact.id],
        archive_state=ArtefactArchiveState.ARCHIVED,
        archived_at=datetime(2026, 4, 1, tzinfo=UTC),
    )
    await raises(r.regenerate_document.execute(ID, a5(doc)), ErrorCode.TENDER_CLOSED)  # ART-7


async def test_a5_admission_refusals() -> None:
    r = WfRig()
    doc = seed(r)
    await raises(
        r.regenerate_document.execute(
            replace(ID, feature_flags=frozenset({READ_ONLY_FLAG})), a5(doc)
        ),
        ErrorCode.TENANT_NOT_ACTIVE,
    )
    r.scope.error = revoked()
    await raises(r.regenerate_document.execute(ID, a5(doc)), ErrorCode.ARTEFACT_NOT_FOUND)  # API-10
    r.scope.error = None
    r.workflows.available = False
    await raises(r.regenerate_document.execute(ID, a5(doc)), ErrorCode.WORKFLOW_ENGINE_UNAVAILABLE)
    assert r.world.runs == {}


async def test_a5_not_generatable_and_provider_side_empty_scope() -> None:
    r = WfRig(
        packs_kw={"default_eligibility": EligibilityDecl(EligibilityState.NOT_GENERATABLE, "no")}
    )
    doc = seed(r)
    await raises(r.regenerate_document.execute(ID, a5(doc)), ErrorCode.GENERATION_NOT_PERMITTED)
    r2 = WfRig()
    doc2 = seed(r2)
    r2.base.provider.error = revoked()  # the resolver, not the authorizer, finds scope empty
    await raises(r2.regenerate_document.execute(ID, a5(doc2)), ErrorCode.ARTEFACT_NOT_FOUND)


async def test_a5_without_an_instruction_is_a_plain_retry() -> None:
    r = WfRig()
    doc = seed(r)
    await r.regenerate_document.execute(ID, a5(doc, instruction=None))
    (run,) = r.world.runs.values()
    assert run.run_input is not None and "instruction" not in run.run_input


# =============================== A-6 ==============================================================
async def run_a6(r: WfRig, doc: Doc, **kw: object):  # noqa: ANN202
    stream = await r.regenerate_section.execute(ID, a6(doc, **kw))
    frames = await drain(stream)
    await r.settle()
    return frames


async def test_a6_mints_a_section_version_and_no_document_version() -> None:
    r = WfRig()
    doc = seed(r)
    frames = await run_a6(r, doc)
    (verdict,) = frames.of(VerdictFrame)
    assert verdict.refusal is None and verdict.fact_conflict is None
    # REGEN-8: a section version, origin regeneration, numbered after the prior one
    new = r.world.completions[-1].section_version
    assert (new.origin, new.version_no, new.manual_regen_count) == (
        ArtefactOrigin.REGENERATION,
        2,
        1,
    )
    assert r.world.completions[-1].artefact_version is None
    assert list(r.world.versions) == [doc.version.id]  # no document version was minted
    assert verdict.section_version_id == new.id and verdict.prior_section_version_id == doc.sv1.id
    assert verdict.artefact_version_id is None and verdict.version_no is None
    sec = r.world.sections[doc.s1.id]
    assert (sec.state, sec.current_version_id) == (SectionState.COMPLETE, new.id)
    art = r.world.artefacts[doc.artefact.id]
    assert (art.status, art.current_version_id) == (ArtefactStatus.COMPLETE, doc.version.id)
    # the same ladder and checklist as an original (REGEN-7)
    assert verdict.groundedness.tiers_run and verdict.completeness.verdict is not None
    assert verdict.evidence_tier is new.evidence_tier
    assert r.store.objects[new.content_ref].startswith(b"Crane erection")  # the new text, in S3
    assert r.store.objects[doc.sv1.content_ref] == OLD  # the prior version is retained


async def test_a6_marks_the_document_versions_superseded_and_says_so_on_the_event() -> None:
    r = WfRig()
    doc = seed(r)
    await run_a6(r, doc)
    assert r.world.versions[doc.version.id].superseded_by_section_edit  # AV-6
    assert r.world.types() == ["GenerationStarted", "SectionCompleted"]  # no ArtefactRegenerated
    started, done = (e.payload for e in r.world.events)
    assert started["synchronous"] is True
    assert done["origin"] == "regeneration" and done["state"] == "complete"
    assert done["superseded_version_ids"] == [str(doc.version.id)]  # LD-31
    assert done["section_ref"] == "sec-1" and done["generation_id"] == started["generation_id"]
    assert (done["sections_complete"], done["sections_total"]) == (2, 2)
    entry = next(
        e for e in r.world.audit.entries if e.draft.entry_type == audit.SECTION_REGENERATED[0]
    )
    assert entry.draft.metadata["origin"] == "regeneration"


async def test_a6_run_row_records_the_regeneration() -> None:
    r = WfRig()
    doc = seed(r)
    await run_a6(r, doc)
    (run,) = r.world.runs.values()
    assert run.origin is ArtefactOrigin.REGENERATION and run.status is RunStatus.COMPLETE
    assert run.base_version_id == doc.version.id and run.workflow_id is None  # synchronous
    assert run.run_input is not None and run.run_input["base_section_version_id"] == str(doc.sv1.id)


async def test_a6_a_stale_base_is_409_before_any_run_exists() -> None:
    r = WfRig()
    doc = seed(r)
    await raises(
        r.regenerate_section.execute(ID, a6(doc, base_version_id=uuid4())),
        ErrorCode.OPTIMISTIC_LOCK_CONFLICT,
    )
    assert r.world.runs == {} and r.gw.stream_calls == []


async def test_a6_two_reviewers_the_second_on_the_old_reading_gets_a_conflict() -> None:
    """REGEN-2: no silent last-write-wins."""
    r = WfRig()
    doc = seed(r)
    await run_a6(r, doc)  # reviewer one regenerates from sv1
    await raises(r.regenerate_section.execute(ID, a6(doc)), ErrorCode.OPTIMISTIC_LOCK_CONFLICT)
    # nor does the superseded document version still serve as a base for that section
    await raises(
        r.regenerate_section.execute(ID, a6(doc, base_version_id=doc.version.id)),
        ErrorCode.OPTIMISTIC_LOCK_CONFLICT,
    )


async def test_a6_accepts_the_current_document_version_while_it_holds_the_section_version() -> None:
    r = WfRig()
    doc = seed(r)
    frames = await run_a6(r, doc, base_version_id=doc.version.id)
    assert frames.of(VerdictFrame)[0].section_version_id is not None


async def test_a6_a_section_in_flight_is_refused_not_overwritten() -> None:
    r = WfRig()
    doc = seed(r)
    r.gw.hold = asyncio.Event()
    stream = await r.regenerate_section.execute(ID, a6(doc))
    await asyncio.sleep(0)
    r.world.sections[doc.s1.id] = replace(
        r.world.sections[doc.s1.id], state=SectionState.GENERATING
    )
    await raises(r.regenerate_section.execute(ID, a6(doc)), ErrorCode.OPTIMISTIC_LOCK_CONFLICT)
    r.gw.hold.set()
    await drain(stream)
    await r.settle()


async def test_a6_two_sections_of_one_artefact_regenerate_independently() -> None:
    r = WfRig()
    doc = seed(r)
    await run_a6(r, doc)
    frames = await run_a6(r, doc, section_ref="sec-2", base_version_id=doc.sv2.id)
    assert frames.of(VerdictFrame)[0].section_version_id is not None  # the artefact stayed at rest


async def test_a6_no_instruction_is_a_plain_regeneration_without_a_refiner_call() -> None:
    r = WfRig()
    doc = seed(r)
    await run_a6(r, doc, instruction=None)
    assert r.gw.instruction_calls == [] and r.world.refinements == []  # nothing to refine


async def test_a6_review_context_is_prompt_input_for_this_request_only() -> None:
    """REGEN-3: never persisted."""
    r = WfRig()
    doc = seed(r)
    await run_a6(r, doc, review_context={"comments": [{"body": "mind the night works"}]})
    prompt = r.gw.stream_calls[0][2]
    assert "mind the night works" in prompt
    assert "mind the night works" not in json.dumps(
        [run.run_input for run in r.world.runs.values()]
    )
    assert all("mind the night works" not in repr(x) for x in r.world.refinements)
    assert all("mind the night works" not in repr(e.payload) for e in r.world.events)


async def test_a6_the_prompt_carries_the_text_being_revised_and_the_refined_instruction() -> None:
    r = WfRig()
    doc = seed(r)
    await run_a6(r, doc)
    system, prompt = r.gw.stream_calls[0][1], r.gw.stream_calls[0][2]
    assert "Old section text" in prompt  # layer 5 content, under the nonce boundary
    assert "Cite the night-work restriction." in prompt
    assert "Reviewer instruction" in prompt and system


# --- the Instruction Refiner on A-6 ---------------------------------------------------------------
async def test_refiner_row_is_recorded_for_the_call_that_ran() -> None:
    r = WfRig()
    doc = seed(r)
    await run_a6(r, doc)
    (row,) = r.world.refinements
    assert row.original_instruction == "Cite the night-work restriction."  # MD-33: always retained
    assert (row.outcome, row.needs_retrieval, row.section_id) == (
        RefinerOutcome.SUCCESS,
        True,
        doc.s1.id,
    )
    assert row.refined_instruction and row.refiner_version.startswith("ir-")
    assert str(USER) not in row.principal_hash and len(row.principal_hash) == 64  # one-way hash
    assert r.gw.instruction_calls  # the model saw the section text and the established facts
    seen = json.loads(r.gw.instruction_calls[0])
    assert seen["established_facts"] == {"access_method": "crane"}
    assert seen["section_text"].startswith("Old section")


async def test_refined_instruction_is_the_retrieval_query_and_the_full_pipeline_runs() -> None:
    r = WfRig()
    doc = seed(r)
    await run_a6(r, doc)
    assert r.base.dense.calls  # REGEN-9: no abbreviated path
    assert r.gw.embeds and "night-work restriction" in r.gw.embeds[0][0]


async def test_style_only_instruction_still_takes_the_full_pipeline() -> None:
    """REGEN-5 reuse is not built (the stored chunks are excerpts), so style takes the pipeline."""
    r = WfRig()
    doc = seed(r)
    r.gw.instruction_text = json.dumps(
        {"instruction_type": "style", "refined_instruction": "Tighter.", "needs_retrieval": False}
    )
    await run_a6(r, doc)
    assert r.world.refinements[0].needs_retrieval is False and r.base.dense.calls


@pytest.mark.parametrize(
    ("error", "reason"),
    [
        (GatewayUnavailable("down"), "unavailable"),
        (GatewayQuotaRefused(30), "quota_refused"),
        (ValueError("x"), "invalid_output"),
    ],
)
async def test_refiner_failure_degrades_to_the_raw_instruction_with_a_marker(
    error: Exception, reason: str
) -> None:
    r = WfRig()
    doc = seed(r)
    r.gw.instruction_error = error if not isinstance(error, ValueError) else None
    if isinstance(error, ValueError):
        r.gw.instruction_text = "not json"
    frames = await run_a6(r, doc)
    (verdict,) = frames.of(VerdictFrame)
    assert verdict.section_version_id is not None  # the regeneration still ran
    marker = {
        "stage": "refine_instruction",
        "reason": reason,
        "applied_fallback": "raw_instruction",
    }
    assert marker in verdict.degradation_markers
    (row,) = r.world.refinements
    assert (row.outcome, row.needs_retrieval, row.refined_instruction) == (
        RefinerOutcome.DEGRADED,
        True,  # IR-1
        None,
    )
    assert "Cite the night-work restriction." in r.gw.stream_calls[0][2]  # the raw text was used
    assert r.world.completions[-1].section_version.degradation_markers  # recorded on the version


async def test_flag_off_skips_the_refiner_entirely_no_marker_no_row() -> None:
    r = WfRig()
    doc = seed(r)
    r.refiner._enabled = False  # llm.refiner.instruction off (REF-2, REF-3)
    frames = await run_a6(r, doc)
    (verdict,) = frames.of(VerdictFrame)
    assert verdict.section_version_id is not None and r.gw.instruction_calls == []
    assert r.world.refinements == []
    assert not any(m["stage"] == "refine_instruction" for m in verdict.degradation_markers)
    assert "Cite the night-work restriction." in r.gw.stream_calls[0][2]  # works on raw input


# --- fact conflicts surfaced by A-6 (D-35, REGEN-4, REGEN-6) --------------------------------------
CONFLICT = json.dumps(
    {
        "instruction_type": "correct",
        "refined_instruction": "Use a hoist for access.",
        "needs_retrieval": True,
        "fact_conflict": {"fact_key": "access_method", "instruction_value": "hoist"},
    }
)


async def test_a_conflict_is_surfaced_as_a_choice_and_nothing_is_generated() -> None:
    r = WfRig()
    doc = seed(r)
    r.gw.instruction_text = CONFLICT
    frames = await run_a6(r, doc, instruction="Say we access by hoist.")
    (verdict,) = frames.items
    assert isinstance(verdict, VerdictFrame) and verdict.fact_conflict is not None
    c = verdict.fact_conflict
    assert (c.fact_key, c.established_value, c.instruction_value) == (
        "access_method",
        "crane",
        "hoist",
    )
    assert c.established_by_section_id == doc.s1.id
    assert set(c.choices) == {"update_fact", "keep_established", "dismiss"}
    assert r.gw.stream_calls == [] and r.base.dense.calls == []  # no retrieval, no draft, no tokens
    (stored,) = r.world.conflicts.values()
    assert stored.id == c.conflict_id and stored.resolution is None
    assert (
        r.world.refinements[0].fact_conflict_id == stored.id
    )  # the dataset row links the conflict
    (run,) = r.world.runs.values()
    assert run.status is RunStatus.CANCELLED  # a refusal, not a failure
    assert r.world.sections[doc.s1.id].current_version_id == doc.sv1.id  # prior version untouched
    assert r.world.sections[doc.s1.id].state is SectionState.COMPLETE


async def test_a_short_form_draft_has_no_facts_so_no_conflict_can_surface() -> None:
    r = WfRig()
    doc = seed(r, kind=ArtefactKind.SECTION_DRAFT, facts=False)
    r.gw.instruction_text = CONFLICT  # the model claims one anyway
    frames = await run_a6(r, doc)
    (verdict,) = frames.of(VerdictFrame)
    assert verdict.fact_conflict is None and verdict.section_version_id is not None  # REGEN-4
    assert r.world.conflicts == {}
    assert json.loads(r.gw.instruction_calls[0])["established_facts"] == {}


async def test_after_keep_established_the_same_request_proceeds_holding_the_fact() -> None:
    r = WfRig()
    doc = seed(r)
    r.gw.instruction_text = CONFLICT
    (first,) = (await run_a6(r, doc)).items
    assert first.fact_conflict is not None  # type: ignore[union-attr]
    await r.resolve_conflict.execute(
        ID,
        doc.artefact.id,
        "sec-1",
        first.fact_conflict.conflict_id,
        ConflictResolution.KEEP_ESTABLISHED,  # type: ignore[union-attr]
    )
    frames = await run_a6(r, doc)  # the regeneration "proceeds as a new request" (LD-26)
    (verdict,) = frames.of(VerdictFrame)
    assert verdict.fact_conflict is None and verdict.section_version_id is not None
    assert "Keep unchanged: access_method = crane" in r.gw.stream_calls[0][2]
    assert len(r.world.conflicts) == 1  # not raised a second time


async def test_after_update_fact_the_same_request_no_longer_conflicts() -> None:
    r = WfRig()
    doc = seed(r)
    r.gw.instruction_text = CONFLICT
    (first,) = (await run_a6(r, doc)).items
    await r.resolve_conflict.execute(
        ID,
        doc.artefact.id,
        "sec-1",
        first.fact_conflict.conflict_id,
        ConflictResolution.UPDATE_FACT,  # type: ignore[union-attr]
    )
    frames = await run_a6(r, doc)
    assert frames.of(VerdictFrame)[0].section_version_id is not None  # hoist is now the fact
    assert len(r.world.conflicts) == 1


# --- failure paths: AS-7 (a regeneration that ends without a version) -----------------------------
async def test_insufficient_retrieval_leaves_the_prior_version_current() -> None:
    r = WfRig()
    doc = seed(r)
    r.base.dense.hits, r.base.lexical.hits = [], []
    (verdict,) = (await run_a6(r, doc)).items
    assert isinstance(verdict, VerdictFrame) and verdict.refusal is not None
    assert verdict.section_version_id is None
    sec = r.world.sections[doc.s1.id]
    assert (sec.state, sec.current_version_id) == (SectionState.COMPLETE, doc.sv1.id)  # AS-7
    assert r.world.artefacts[doc.artefact.id].status is ArtefactStatus.COMPLETE
    (run,) = r.world.runs.values()
    assert run.status is RunStatus.CANCELLED  # the run records the attempt
    assert r.world.versions[doc.version.id].superseded_by_section_edit is False  # nothing changed
    assert "SectionCompleted" not in r.world.types()  # no event for a regeneration with no version


async def test_a_gateway_failure_before_the_first_frame_is_http_and_changes_no_state() -> None:
    r = WfRig()
    doc = seed(r)
    r.gw.before_first = GatewayUnavailable("down")
    with pytest.raises(Exception) as exc:
        await r.regenerate_section.execute(ID, a6(doc))
    assert getattr(exc.value, "code", None) is ErrorCode.INFERENCE_UNAVAILABLE or isinstance(
        exc.value, GatewayUnavailable
    )
    await r.settle()
    sec = r.world.sections[doc.s1.id]
    assert (sec.state, sec.current_version_id) == (SectionState.COMPLETE, doc.sv1.id)
    (run,) = r.world.runs.values()
    assert run.status in (RunStatus.CANCELLED, RunStatus.FAILED)
    assert r.world.artefacts[doc.artefact.id].status is ArtefactStatus.COMPLETE


async def test_a_failure_mid_stream_fails_the_run_and_keeps_the_prior_version() -> None:
    r = WfRig()
    doc = seed(r)
    r.gw.fail_after = (1, GatewayUnavailable("dropped"))
    frames = await run_a6(r, doc)
    assert frames.items[-1].__class__.__name__ == "ErrorFrame"
    sec = r.world.sections[doc.s1.id]
    assert (sec.state, sec.current_version_id) == (SectionState.COMPLETE, doc.sv1.id)  # AS-7
    (run,) = r.world.runs.values()
    assert (
        run.status is RunStatus.FAILED
        and run.failure_reason is RunFailureReason.INFERENCE_UNAVAILABLE
    )
    assert r.world.artefacts[doc.artefact.id].status is ArtefactStatus.COMPLETE


async def test_a6_scope_and_tenant_refusals() -> None:
    r = WfRig()
    doc = seed(r)
    r.scope.error = revoked()
    await raises(r.regenerate_section.execute(ID, a6(doc)), ErrorCode.ARTEFACT_NOT_FOUND)  # API-10
    r.scope.error = None
    await raises(
        r.regenerate_section.execute(
            replace(ID, feature_flags=frozenset({READ_ONLY_FLAG})), a6(doc)
        ),
        ErrorCode.TENANT_NOT_ACTIVE,
    )
    r.gate.closed.add(TENDER)
    await raises(r.regenerate_section.execute(ID, a6(doc)), ErrorCode.TENDER_CLOSED)
    r.gate.closed.clear()
    await raises(
        r.regenerate_section.execute(ID, a6(doc, section_ref="nope")), ErrorCode.ARTEFACT_NOT_FOUND
    )
    assert r.world.runs == {}


async def test_a6_an_artefact_with_a_run_in_flight_is_a_409() -> None:
    r = WfRig()
    doc = seed(r)
    r.world.artefacts[doc.artefact.id] = replace(
        r.world.artefacts[doc.artefact.id], status=ArtefactStatus.GENERATING
    )
    await raises(r.regenerate_section.execute(ID, a6(doc)), ErrorCode.OPTIMISTIC_LOCK_CONFLICT)


# =============================== A-7 ==============================================================
def open_conflict(r: WfRig, doc: Doc, **kw: object) -> FactConflict:
    c = FactConflict(
        uuid4(), TENANT, doc.artefact.id, doc.s1.id, "access_method", "crane", "hoist", doc.s1.id,
        **kw,  # type: ignore[arg-type]
    )  # fmt: skip
    r.world.conflicts[c.id] = c
    return c


async def resolve(r: WfRig, doc: Doc, c: FactConflict, res: ConflictResolution, ref: str = "sec-1"):  # noqa: ANN202
    return await r.resolve_conflict.execute(ID, doc.artefact.id, ref, c.id, res)


async def test_a7_keep_established_changes_no_fact_and_flags_nothing() -> None:
    r = WfRig()
    doc = seed(r)
    c = open_conflict(r, doc)
    out = await resolve(r, doc, c, ConflictResolution.KEEP_ESTABLISHED)
    assert out.proceeds and out.flagged_section_ids == ()
    assert [f.value for f in r.world.facts] == ["crane"]
    assert r.world.conflicts[c.id].resolution is ConflictResolution.KEEP_ESTABLISHED
    assert r.world.conflicts[c.id].resolved_at is not None
    assert all(not s.flagged_for_review for s in r.world.sections.values())
    assert r.world.runs == {} and r.world.events == []  # nothing regenerates here (LD-26)
    (entry,) = r.world.audit.entries
    assert entry.draft.entry_type == audit.FACT_CONFLICT_RESOLVED[0]
    assert entry.draft.metadata == {
        "conflict_id": str(c.id),
        "fact_key": "access_method",
        "resolution": "keep_established",
    }


async def test_a7_update_fact_supersedes_inserts_and_flags_but_regenerates_nothing() -> None:
    r = WfRig()
    doc = seed(r)
    c = open_conflict(r, doc)
    out = await resolve(r, doc, c, ConflictResolution.UPDATE_FACT)
    old, new = r.world.facts
    assert old.value == "crane" and old.superseded_at is not None  # DF-2: marked, not overwritten
    assert (new.value, new.superseded_at, new.fact_key) == ("hoist", None, "access_method")
    assert new.established_by_section_id == doc.s1.id
    # the other section is flagged for review (not regenerated); the edited one is the reviewer's
    assert out.flagged_section_ids == (doc.s2.id,)
    flagged = r.world.sections[doc.s2.id]
    assert flagged.flagged_for_review and flagged.flagged_reason == "fact_updated:access_method"
    assert not r.world.sections[doc.s1.id].flagged_for_review
    assert r.world.sections[doc.s2.id].current_version_id == doc.sv2.id  # not regenerated
    kinds = [e.draft.entry_type for e in r.world.audit.entries]
    assert kinds == [audit.FACT_CONFLICT_RESOLVED[0], audit.SECTION_FLAGGED[0]]
    assert r.world.runs == {} and r.world.events == []


async def test_a7_update_fact_updates_the_dataset_row_of_the_refinement() -> None:
    r = WfRig()
    doc = seed(r)
    r.gw.instruction_text = CONFLICT
    (first,) = (await run_a6(r, doc)).items
    await r.resolve_conflict.execute(
        ID,
        doc.artefact.id,
        "sec-1",
        first.fact_conflict.conflict_id,
        ConflictResolution.DISMISS,  # type: ignore[union-attr]
    )
    assert r.world.refinements[0].conflict_resolution is ConflictResolution.DISMISS


async def test_a7_dismiss_closes_the_conflict_and_changes_nothing_else() -> None:
    r = WfRig()
    doc = seed(r)
    c = open_conflict(r, doc)
    out = await resolve(r, doc, c, ConflictResolution.DISMISS)
    assert not out.proceeds and out.flagged_section_ids == ()  # LD-32
    assert [f.value for f in r.world.facts] == ["crane"]
    assert r.world.conflicts[c.id].resolution is ConflictResolution.DISMISS
    assert all(not s.flagged_for_review for s in r.world.sections.values())


async def test_a7_a_second_reviewer_resolving_differently_is_a_409_and_overwrites_nothing() -> None:
    r = WfRig()
    doc = seed(r)
    c = open_conflict(r, doc)
    await resolve(r, doc, c, ConflictResolution.KEEP_ESTABLISHED)
    err = await raises(
        resolve(r, doc, c, ConflictResolution.UPDATE_FACT), ErrorCode.WORKFLOW_STATE_CONFLICT
    )
    assert err.extensions == {"resolution": "keep_established"}  # the recorded resolution
    assert r.world.conflicts[c.id].resolution is ConflictResolution.KEEP_ESTABLISHED
    assert [f.value for f in r.world.facts] == ["crane"]


async def test_a7_is_atomic_an_audit_failure_rolls_back_the_resolution_and_the_fact() -> None:
    r = WfRig()
    doc = seed(r)
    c = open_conflict(r, doc)
    r.world.audit.fail = True
    with pytest.raises(RuntimeError):
        await resolve(r, doc, c, ConflictResolution.UPDATE_FACT)
    assert r.world.conflicts[c.id].resolution is None  # still open: the reviewer can retry
    assert [(f.value, f.superseded_at) for f in r.world.facts] == [("crane", None)]
    assert all(not s.flagged_for_review for s in r.world.sections.values())


async def test_a7_lookups_that_find_nothing() -> None:
    r = WfRig()
    doc = seed(r)
    c = open_conflict(r, doc)
    await raises(
        r.resolve_conflict.execute(
            ID, doc.artefact.id, "sec-1", uuid4(), ConflictResolution.DISMISS
        ),
        ErrorCode.FACT_CONFLICT_NOT_FOUND,
    )
    await raises(  # a conflict of another section is not this section's
        resolve(r, doc, c, ConflictResolution.DISMISS, ref="sec-2"),
        ErrorCode.FACT_CONFLICT_NOT_FOUND,
    )
    await raises(
        resolve(r, doc, c, ConflictResolution.DISMISS, ref="nope"), ErrorCode.ARTEFACT_NOT_FOUND
    )
    await raises(
        r.resolve_conflict.execute(ID, uuid4(), "sec-1", c.id, ConflictResolution.DISMISS),
        ErrorCode.ARTEFACT_NOT_FOUND,
    )


async def test_a7_a_short_form_artefact_has_no_conflicts() -> None:
    r = WfRig()
    doc = seed(r, kind=ArtefactKind.SECTION_DRAFT, facts=False)
    c = open_conflict(r, doc)
    await raises(resolve(r, doc, c, ConflictResolution.DISMISS), ErrorCode.FACT_CONFLICT_NOT_FOUND)


async def test_a7_scope_tender_and_a_conflict_of_another_artefact() -> None:
    r = WfRig()
    doc = seed(r)
    other = seed(r)
    c = open_conflict(r, other)  # belongs to another artefact
    await raises(resolve(r, doc, c, ConflictResolution.DISMISS), ErrorCode.FACT_CONFLICT_NOT_FOUND)
    mine = open_conflict(r, doc)
    r.scope.error = revoked()
    await raises(resolve(r, doc, mine, ConflictResolution.DISMISS), ErrorCode.ARTEFACT_NOT_FOUND)
    r.scope.error = None
    from datetime import UTC, datetime

    from rag.domain.shared.enums import ArtefactArchiveState

    r.world.artefacts[doc.artefact.id] = replace(
        r.world.artefacts[doc.artefact.id],
        archive_state=ArtefactArchiveState.ARCHIVED,
        archived_at=datetime(2026, 4, 1, tzinfo=UTC),
    )
    await raises(
        resolve(r, doc, mine, ConflictResolution.DISMISS), ErrorCode.TENDER_CLOSED
    )  # ART-7


async def test_a6_never_writes_the_artefact_row_so_it_cannot_undo_a_concurrent_cancel() -> None:
    r = WfRig()
    doc = seed(r)

    async def forbidden(*a: object, **k: object) -> None:
        raise AssertionError("a section regeneration wrote the artefact row from a stale copy")

    r.writes.save_state = forbidden  # type: ignore[method-assign]
    frames = await run_a6(r, doc)
    assert frames.of(VerdictFrame)[0].section_version_id is not None


async def test_a6_on_an_artefact_with_no_document_version_is_refused() -> None:
    """ck_run_base: a regeneration run names a base document version; there is none yet."""
    r = WfRig()
    doc = seed(r)
    r.world.artefacts[doc.artefact.id] = replace(
        r.world.artefacts[doc.artefact.id],
        current_version_id=None,
        status=ArtefactStatus.FAILED,
    )
    await raises(r.regenerate_section.execute(ID, a6(doc)), ErrorCode.OPTIMISTIC_LOCK_CONFLICT)
    assert r.world.runs == {}


async def test_a7_a_closed_tender_is_409_before_the_archive_has_run() -> None:
    """COL-14: the closure itself is read, not the archive state (which lags it)."""
    r = WfRig()
    doc = seed(r)
    c = open_conflict(r, doc)
    r.gate.closed.add(TENDER)  # the artefact is not archived yet
    await raises(resolve(r, doc, c, ConflictResolution.UPDATE_FACT), ErrorCode.TENDER_CLOSED)
    assert r.world.conflicts[c.id].resolution is None and [f.value for f in r.world.facts] == [
        "crane"
    ]
    assert all(not s.flagged_for_review for s in r.world.sections.values())
