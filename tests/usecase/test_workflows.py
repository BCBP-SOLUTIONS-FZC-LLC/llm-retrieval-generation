"""W-1 to W-8 over fakes: admission then start, the gate loop, cancel, resume, rematerialise."""

from __future__ import annotations

from dataclasses import replace
from uuid import uuid4

import pytest
from tests.support_workflows import (
    SCHEMA,
    TENANT,
    TENDER,
    USER,
    FakeWorkflows,
    WfRig,
    engine_down,
    make_run,
    seed_document,
)

from rag.application.events.consumers import ResumeSuspendedRuns
from rag.application.events.ports import SuspendedRun
from rag.application.workflows.signals import OutlineEdit, OutlineEntry
from rag.application.workflows.start import StartExtractionCommand, StartGenerationCommand
from rag.domain.generation import audit
from rag.domain.packs.generation import EligibilityDecl
from rag.domain.shared.enums import (
    ArtefactKind,
    ArtefactStatus,
    EligibilityState,
    GenerationMode,
    OutlineState,
    RunFailureReason,
    RunStage,
    RunStatus,
    SectionState,
)
from rag.domain.shared.errors import DomainError, ErrorCode
from rag.domain.shared.identity import READ_ONLY_FLAG, RequestIdentity, ResidencyMode

ID = RequestIdentity(tenant_id=TENANT, principal_id=USER)


def revoked() -> DomainError:
    """What scope resolution says for an empty scope (the authorizer's CG-8 verdict)."""
    return DomainError(ErrorCode.COLLECTION_NOT_FOUND)


def cmd(**kw: object) -> StartGenerationCommand:
    base: dict[str, object] = dict(
        tender_id=TENDER, capability="method_statement", idempotency_key="k-1"
    )
    return StartGenerationCommand(**{**base, **kw})  # type: ignore[arg-type]


async def raises(coro, code: ErrorCode) -> DomainError:  # noqa: ANN001
    with pytest.raises(DomainError) as exc:
        await coro
    assert exc.value.code is code, exc.value
    return exc.value


# =============================== W-1 ==============================================================
async def test_w1_commits_the_shell_then_starts_under_the_artefact_id() -> None:
    r = WfRig()
    handle = await r.start.execute(ID, cmd(target_ref="tender:1:proposal"))
    run = next(iter(r.world.runs.values()))
    art = r.world.artefacts[run.artefact_id]
    assert (handle.generation_id, handle.artefact_id, handle.replayed) == (run.id, art.id, False)
    assert (handle.status, art.status, art.kind) == (
        RunStatus.PENDING,
        ArtefactStatus.PENDING,
        ArtefactKind.DOCUMENT,
    )
    assert run.workflow_id == str(art.id)  # RUN-2
    assert r.world.sections == {}  # a document's sections are the outline's rows (RUN-4)
    (start,) = r.workflows.starts
    assert (start.workflow_id, start.workflow_type, start.task_queue) == (
        str(art.id),
        "GenerationWorkflow",
        "q",
    )
    assert start.run_input == run.run_input  # the stored input is the start argument (RUN-8)
    assert r.world.types() == ["GenerationStarted"]
    started = r.world.events[0].payload
    assert started["synchronous"] is False and started["target_ref"] == "tender:1:proposal"
    assert (handle.domain_profile_version, handle.language_pack_version) == ("dp-1", "lp-1")


async def test_w1_input_is_identifiers_and_a_seed_only() -> None:
    r = WfRig()
    await r.start.execute(ID, cmd())
    body = next(iter(r.world.runs.values())).run_input
    assert body is not None
    assert not {"departments", "roles", "scope_token", "scope"} & set(body)
    assert body["scope_seed"]["tender_id"] == str(TENDER)
    assert body["origin"] == "original" and body["kind"] == "document"
    assert body["domain_profile_version"] == "dp-1"  # pinned at start (ASYNC-3)


async def test_w1_ordering_is_commit_before_start() -> None:
    r = WfRig()

    class Spy(FakeWorkflows):
        async def start(self, request):  # noqa: ANN001, ANN202
            assert len(r.world.runs) == 1  # the row was committed before the engine heard of it
            return await super().start(request)

    r.workflows = Spy()
    r.starter._workflows = r.workflows
    await r.start.execute(ID, cmd())
    assert len(r.workflows.starts) == 1


async def test_w1_replay_of_the_same_request_returns_the_handle_and_no_second_run() -> None:
    r = WfRig()
    first = await r.start.execute(ID, cmd())
    run = r.world.runs[first.generation_id]
    r.world.runs[run.id] = replace(run, status=RunStatus.GENERATING)  # the worker took it
    again = await r.start.execute(ID, cmd())
    assert again.replayed and again.generation_id == first.generation_id
    assert again.status is RunStatus.GENERATING  # the stored status, not a restart
    assert len(r.world.runs) == 1 and len(r.workflows.starts) == 1
    assert r.world.types() == ["GenerationStarted"]  # one admission, one event


async def test_w1_same_key_other_body_is_a_conflict_never_a_second_run() -> None:
    r = WfRig()
    await r.start.execute(ID, cmd())
    await raises(r.start.execute(ID, cmd(capability="other")), ErrorCode.IDEMPOTENCY_KEY_CONFLICT)
    assert len(r.world.runs) == 1  # RUN-3


async def test_w1_failed_start_leaves_a_recoverable_pending_row() -> None:
    r = WfRig()
    r.workflows.fail_start = engine_down()
    await raises(r.start.execute(ID, cmd()), ErrorCode.WORKFLOW_ENGINE_UNAVAILABLE)
    (run,) = r.world.runs.values()
    art = r.world.artefacts[run.artefact_id]
    assert (run.status, art.status) == (RunStatus.PENDING, ArtefactStatus.PENDING)  # ASYNC-2
    assert run.workflow_id and run.run_input  # everything the orphan sweep needs to start it again
    assert r.world.types() == ["GenerationStarted"]  # the committed admission stands


async def test_w1_retry_after_a_failed_start_starts_the_same_run() -> None:
    r = WfRig()
    r.workflows.fail_start = engine_down()
    await raises(r.start.execute(ID, cmd()), ErrorCode.WORKFLOW_ENGINE_UNAVAILABLE)
    r.workflows.fail_start = None
    retry = await r.start.execute(ID, cmd())  # the client's retry with the same key
    assert retry.replayed and len(r.world.runs) == 1  # a handle, not a second artefact
    assert [s.workflow_id for s in r.workflows.starts] == [str(retry.artefact_id)]  # started now


async def test_w1_with_no_engine_configured_is_503_before_anything_is_written() -> None:
    r = WfRig()
    r.workflows.available = False
    await raises(r.start.execute(ID, cmd()), ErrorCode.WORKFLOW_ENGINE_UNAVAILABLE)
    assert r.world.runs == {} and r.world.artefacts == {} and r.world.events == []


async def test_w1_refusals_leave_nothing_behind() -> None:
    r = WfRig()
    await raises(
        r.start.execute(replace(ID, feature_flags=frozenset({READ_ONLY_FLAG})), cmd()),
        ErrorCode.TENANT_NOT_ACTIVE,
    )
    r.gate.closed.add(TENDER)
    await raises(r.start.execute(ID, cmd()), ErrorCode.TENDER_CLOSED)
    r.gate.closed.clear()
    r.probe.down = True
    await raises(r.start.execute(ID, cmd()), ErrorCode.ARTEFACT_STORE_UNWRITABLE)
    r.probe.down = False
    assert r.world.runs == {} and r.world.events == [] and r.workflows.starts == []


async def test_w1_not_generatable_is_refused_before_admission() -> None:
    r = WfRig(
        packs_kw={"default_eligibility": EligibilityDecl(EligibilityState.NOT_GENERATABLE, "no")}
    )
    await raises(r.start.execute(ID, cmd()), ErrorCode.GENERATION_NOT_PERMITTED)
    assert r.world.runs == {}


async def test_w1_a_cancelled_runs_key_is_released_for_a_retry() -> None:
    r = WfRig()
    first = await r.start.execute(ID, cmd())
    r.world.runs[first.generation_id] = replace(
        r.world.runs[first.generation_id], status=RunStatus.CANCELLED
    )
    second = await r.start.execute(ID, cmd())
    assert not second.replayed and second.generation_id != first.generation_id


async def test_w1_clamp_and_pack_versions_are_recorded_on_the_run() -> None:
    r = WfRig(
        packs_kw={
            "default_eligibility": EligibilityDecl(
                EligibilityState.PARTIALLY_GENERATABLE, "profile_withholds_unattended_completion"
            )
        }
    )
    handle = await r.start.execute(ID, cmd(requested_mode=GenerationMode.AUTOMATION))
    run = r.world.runs[handle.generation_id]
    assert run.clamped and run.effective_mode is GenerationMode.ASSISTANCE
    assert (
        handle.eligibility.clamped
        and handle.eligibility.requested_mode is GenerationMode.AUTOMATION
    )


@pytest.mark.parametrize(
    ("identity", "queue"),
    [
        (ID, "q"),
        (replace(ID, plan="enterprise"), "q-ent"),
        (replace(ID, residency_mode=ResidencyMode.STRICT), "q-strict"),
    ],
)
async def test_w1_task_queue_is_chosen_from_plan_and_residency(
    identity: RequestIdentity, queue: str
) -> None:
    r = WfRig()
    await r.start.execute(identity, cmd())
    assert r.workflows.starts[0].task_queue == queue  # WP-10


async def test_w1_the_gate_is_skipped_only_where_the_pack_permits() -> None:
    r = WfRig()
    h = await r.start.execute(ID, cmd(outline_gate=False))
    assert r.world.runs[h.generation_id].run_input["outline_gate"] is False  # type: ignore[index]
    strict = WfRig(packs_kw={"outline_gate_required": True})
    h2 = await strict.start.execute(ID, cmd(outline_gate=False))
    assert strict.world.runs[h2.generation_id].run_input["outline_gate"] is True  # type: ignore[index]


# =============================== W-6 ==============================================================
def xcmd(**kw: object) -> StartExtractionCommand:
    base: dict[str, object] = dict(tender_id=TENDER, schema_ref=SCHEMA.ref, idempotency_key="x-1")
    return StartExtractionCommand(**{**base, **kw})  # type: ignore[arg-type]


async def test_w6_starts_an_extraction_workflow_with_its_one_section() -> None:
    r = WfRig()
    h = await r.start_extraction.execute(ID, xcmd(trigger_document_version_id="dv-9"))
    run = r.world.runs[h.generation_id]
    art = r.world.artefacts[run.artefact_id]
    assert art.kind is ArtefactKind.EXTRACTION
    (section,) = r.world.sections.values()
    assert (section.section_type, section.ordinal) == (SCHEMA.ref, 1)
    assert r.workflows.starts[0].workflow_type == "ExtractionWorkflow"
    assert run.run_input["schema_ref"] == SCHEMA.ref  # type: ignore[index]
    assert run.run_input["trigger_document_version_id"] == "dv-9"  # type: ignore[index]
    assert run.run_input["outline_gate"] is False  # type: ignore[index]  # an extraction has no gate


async def test_w6_unknown_schema_is_pack_invalid_and_writes_nothing() -> None:
    r = WfRig()
    await raises(r.start_extraction.execute(ID, xcmd(schema_ref="nope")), ErrorCode.PACK_INVALID)
    assert r.world.runs == {}


async def test_w6_replay_and_conflict() -> None:
    r = WfRig()
    first = await r.start_extraction.execute(ID, xcmd())
    again = await r.start_extraction.execute(ID, xcmd())
    assert again.replayed and again.generation_id == first.generation_id
    await raises(
        r.start_extraction.execute(ID, xcmd(trigger_document_version_id="other")),
        ErrorCode.IDEMPOTENCY_KEY_CONFLICT,
    )


# =============================== W-2 ==============================================================
async def test_w2_state_comes_from_our_tables_and_never_from_the_engine() -> None:
    r = WfRig()
    art, run, secs = seed_document(r.world, sections=4, complete=2)
    r.world.sections[secs[2].id] = replace(
        secs[2], state=SectionState.GAP, gap_reason="insufficient_twice"
    )
    r.world.sections[secs[3].id] = replace(
        secs[3], state=SectionState.FAILED, failure_reason=RunFailureReason.INFERENCE_UNAVAILABLE
    )
    r.world.runs[run.id] = replace(run, outline_ready_at=run.created_at)
    r.world.scope_narrowed = True
    state = await r.state.execute(ID, run.id)
    assert (state.status, state.outline.state, state.outline.version) == (
        "awaiting_outline_approval",
        "ready",
        1,
    )
    assert [s.ref for s in state.outline.sections] == ["sec-1", "sec-2", "sec-3", "sec-4"]
    assert (
        state.progress.total,
        state.progress.complete,
        state.progress.gap,
        state.progress.failed,
    ) == (4, 2, 1, 1)
    by = {s.ref: s for s in state.sections}
    assert by["sec-3"].gap_reason == "insufficient_twice" and by["sec-3"].failure_reason is None
    assert by["sec-4"].failure_reason == "inference_unavailable" and by["sec-4"].gap_reason is None
    assert by["sec-1"].section_version_id is not None and by["sec-1"].evidence_tier == "PARTIAL"
    assert state.scope_narrowed and state.coherence.verdict == "not_computed"
    assert r.workflows.starts == [] and r.workflows.signals == []  # ASYNC-5


async def test_w2_established_facts_are_the_current_ones() -> None:
    from tests.unit.test_workflows_domain import _fact

    r = WfRig()
    art, run, secs = seed_document(r.world)
    r.world.facts += [
        _fact("access_method", "crane"),
        _fact("old", "x", superseded_at=run.created_at),
    ]
    r.world.facts = [replace(f, artefact_id=art.id) for f in r.world.facts]
    state = await r.state.execute(ID, run.id)
    assert state.established_facts == {"access_method": "crane"}


async def test_w2_unknown_run_and_empty_scope_are_the_same_404() -> None:
    r = WfRig()
    _, run, _ = seed_document(r.world)
    await raises(r.state.execute(ID, uuid4()), ErrorCode.ARTEFACT_NOT_FOUND)
    r.scope.error = revoked()
    await raises(r.state.execute(ID, run.id), ErrorCode.ARTEFACT_NOT_FOUND)  # API-10
    assert r.world.runs[run.id].status is RunStatus.AWAITING_OUTLINE_APPROVAL  # a read ends nothing


# =============================== W-3 / W-4 ========================================================
async def test_w3_approve_commits_the_row_event_then_signals() -> None:
    r = WfRig()
    art, run, _ = seed_document(r.world)
    ack = await r.approve.execute(ID, run.id, 1)
    after = r.world.runs[run.id]
    assert (after.status, after.outline_state, after.current_stage) == (
        RunStatus.GENERATING,
        OutlineState.APPROVED,
        RunStage.STAGE1,
    )
    assert (ack.status, ack.outline_state) == (RunStatus.GENERATING, OutlineState.APPROVED)
    (event,) = r.world.events
    assert event.type == "GenerationProgressed"
    assert event.payload["progress_seq"] == after.record_version  # EVT-13: the very write's seq
    assert event.payload["run_status"] == "generating" and event.payload["stage"] == "stage1"
    assert r.workflows.signals == [(run.workflow_id, "approve_outline", {"outline_version": 1})]
    assert r.scope.calls[-1][2].value == "generate"  # ASYNC-4: scope re-resolved at generate


async def test_w3_the_signal_is_raised_after_the_commit_and_never_before() -> None:
    r = WfRig()
    _, run, _ = seed_document(r.world)
    r.events.fail_on = "GenerationProgressed"  # the transaction fails at its last statement
    with pytest.raises(RuntimeError):
        await r.approve.execute(ID, run.id, 1)
    assert r.world.runs[run.id].status is RunStatus.AWAITING_OUTLINE_APPROVAL  # rolled back
    assert r.workflows.signals == []  # nothing was signalled for a state that never committed


@pytest.mark.parametrize("version", [0 + 2, 7])
async def test_w3_a_stale_outline_version_is_a_409(version: int) -> None:
    r = WfRig()
    _, run, _ = seed_document(r.world, outline_version=1)
    await raises(r.approve.execute(ID, run.id, version), ErrorCode.WORKFLOW_STATE_CONFLICT)
    assert r.workflows.signals == [] and r.world.events == []


async def test_w3_a_run_not_awaiting_approval_is_a_409() -> None:
    r = WfRig()
    _, run, _ = seed_document(r.world, status=RunStatus.GENERATING, outline_state=OutlineState.NONE)
    await raises(r.approve.execute(ID, run.id, 1), ErrorCode.WORKFLOW_STATE_CONFLICT)


async def test_w3_an_unknown_run_is_404() -> None:
    await raises(WfRig().approve.execute(ID, uuid4(), 1), ErrorCode.ARTEFACT_NOT_FOUND)


async def test_w3_not_generatable_now_is_403_and_the_run_is_untouched() -> None:
    r = WfRig(
        packs_kw={"default_eligibility": EligibilityDecl(EligibilityState.NOT_GENERATABLE, "no")}
    )
    _, run, _ = seed_document(r.world)
    await raises(r.approve.execute(ID, run.id, 1), ErrorCode.GENERATION_NOT_PERMITTED)
    assert r.world.runs[run.id].status is RunStatus.AWAITING_OUTLINE_APPROVAL
    assert r.workflows.signals == []


async def test_w3_signal_failure_after_commit_is_503_and_a_repeat_raises_it_again() -> None:
    r = WfRig()
    _, run, _ = seed_document(r.world)
    r.workflows.fail_signal = engine_down()
    await raises(r.approve.execute(ID, run.id, 1), ErrorCode.WORKFLOW_ENGINE_UNAVAILABLE)
    assert r.world.runs[run.id].outline_state is OutlineState.APPROVED  # the row committed
    r.workflows.fail_signal = None
    ack = await r.approve.execute(ID, run.id, 1)  # not a 409: the lost signal is re-raised
    assert ack.outline_state is OutlineState.APPROVED
    assert len(r.workflows.signals) == 1 and len(r.world.events) == 1  # one event, one signal


async def test_w3_an_empty_scope_ends_the_run_scope_revoked_and_answers_404() -> None:
    r = WfRig()
    art, run, secs = seed_document(r.world, sections=3, complete=1, generating=1)
    r.scope.error = revoked()
    await raises(r.approve.execute(ID, run.id, 1), ErrorCode.ARTEFACT_NOT_FOUND)
    after = r.world.runs[run.id]
    assert (after.status, after.failure_reason) == (
        RunStatus.FAILED,
        RunFailureReason.SCOPE_REVOKED,
    )
    assert r.world.artefacts[art.id].status is ArtefactStatus.FAILED
    states = sorted(s.state.value for s in r.world.sections.values())
    assert states == ["complete", "failed", "pending"]  # AS-8: generating closed, pending stays
    assert r.world.types() == ["GenerationFailed"]
    failed = r.world.events[0].payload
    assert failed["reason"] == "scope_revoked" and failed["sections_durable"] is True
    assert (failed["sections_complete"], failed["sections_total"]) == (1, 3)
    assert [e.draft.entry_type for e in r.world.audit.entries] == [audit.GENERATION_FAILED[0]]
    assert r.workflows.signals[0][1] == "cancel"  # the workflow is told to stop, best effort


async def test_w3_scope_revoked_is_idempotent_on_a_repeat() -> None:
    r = WfRig()
    _, run, _ = seed_document(r.world)
    r.scope.error = revoked()
    await raises(r.approve.execute(ID, run.id, 1), ErrorCode.ARTEFACT_NOT_FOUND)
    await raises(r.approve.execute(ID, run.id, 1), ErrorCode.ARTEFACT_NOT_FOUND)
    assert r.world.types() == ["GenerationFailed"]  # one verdict, one event


async def test_a_scope_store_outage_ends_nothing() -> None:
    r = WfRig()
    _, run, _ = seed_document(r.world)
    r.scope.error = DomainError(ErrorCode.SCOPE_STORE_UNAVAILABLE)
    await raises(r.approve.execute(ID, run.id, 1), ErrorCode.SCOPE_STORE_UNAVAILABLE)
    assert r.world.runs[run.id].status is RunStatus.AWAITING_OUTLINE_APPROVAL  # not a revocation


async def test_w4_revise_increments_the_version_and_signals_the_edited_outline() -> None:
    r = WfRig()
    _, run, _ = seed_document(r.world, outline_version=2)
    edit = OutlineEdit(2, (OutlineEntry("sec-1", "introduction"), OutlineEntry("sec-9", "risk")))
    ack = await r.revise.execute(ID, run.id, edit)
    after = r.world.runs[run.id]
    assert (after.status, after.outline_state, after.outline_version, after.current_stage) == (
        RunStatus.OUTLINE_BUILDING,
        OutlineState.REVISING,
        3,
        RunStage.OUTLINE,
    )
    assert ack.outline_version == 3
    (sig,) = r.workflows.signals
    assert sig[1] == "revise_outline" and sig[2] == {
        "outline_version": 3,
        "sections": [{"ref": "sec-1", "type": "introduction"}, {"ref": "sec-9", "type": "risk"}],
    }
    assert r.world.events[0].payload["outline_state"] == "revising"


async def test_w3_w4_loop_revise_then_the_activity_then_approve_the_new_version() -> None:
    r = WfRig()
    _, run, _ = seed_document(r.world, outline_version=1)
    await r.revise.execute(ID, run.id, OutlineEdit(1))
    await raises(r.approve.execute(ID, run.id, 2), ErrorCode.WORKFLOW_STATE_CONFLICT)  # building
    stored = r.world.runs[run.id]  # the outline activity runs again (not this service)
    moved = replace(stored)
    moved.outline_started()
    moved.outline_written(stored.created_at)  # type: ignore[arg-type]
    r.world.runs[run.id] = moved
    await raises(r.approve.execute(ID, run.id, 1), ErrorCode.WORKFLOW_STATE_CONFLICT)  # stale
    await r.approve.execute(ID, run.id, 2)
    assert r.world.runs[run.id].status is RunStatus.GENERATING
    assert [s[1] for s in r.workflows.signals] == ["revise_outline", "approve_outline"]


async def test_w4_stale_version_and_invalid_outline_are_409_and_change_nothing() -> None:
    r = WfRig()
    _, run, _ = seed_document(r.world, outline_version=2)
    await raises(r.revise.execute(ID, run.id, OutlineEdit(1)), ErrorCode.WORKFLOW_STATE_CONFLICT)
    dup = OutlineEdit(2, (OutlineEntry("a", "t"), OutlineEntry("a", "t")))
    await raises(r.revise.execute(ID, run.id, dup), ErrorCode.WORKFLOW_STATE_CONFLICT)
    assert r.world.runs[run.id].outline_version == 2 and r.workflows.signals == []


# =============================== W-5 ==============================================================
async def test_w5_cancel_closes_run_artefact_and_in_flight_sections_in_one_transaction() -> None:
    r = WfRig()
    art, run, secs = seed_document(
        r.world, status=RunStatus.GENERATING, outline_state=OutlineState.APPROVED,
        sections=4, complete=1, generating=2,
    )  # fmt: skip
    # one in-flight section already has a version (a regeneration inside the run)
    sv = next(iter(r.world.section_versions.values()))
    r.world.sections[secs[2].id] = replace(secs[2], current_version_id=sv.id)
    ack = await r.cancel.execute(ID, run.id, "superseded")
    assert ack.status is RunStatus.CANCELLED
    assert r.world.runs[run.id].status is RunStatus.CANCELLED
    assert r.world.artefacts[art.id].status is ArtefactStatus.CANCELLED  # ART-6
    got = {s.ordinal: s for s in r.world.sections.values()}
    assert got[1].state is SectionState.COMPLETE  # completed work stays durable
    assert got[2].state is SectionState.FAILED and got[2].failure_reason is not None  # AS-8
    assert got[3].state is SectionState.COMPLETE  # in flight with a version: back to complete
    assert got[4].state is SectionState.PENDING  # pending stays pending
    (entry,) = r.world.audit.entries
    assert entry.draft.entry_type == audit.GENERATION_CANCELLED[0]
    assert entry.draft.metadata == {"generation_run_id": str(run.id)}
    (event,) = r.world.events
    assert event.type == "GenerationCancelled" and event.payload["reason"] == "superseded"
    assert (event.payload["sections_complete"], event.payload["sections_total"]) == (2, 4)
    assert event.payload["sections_durable"] is True
    assert r.workflows.signals == [(run.workflow_id, "cancel", {"reason": "superseded"})]


async def test_w5_is_atomic_a_failure_in_the_last_statement_leaves_nothing_cancelled() -> None:
    r = WfRig()
    art, run, secs = seed_document(
        r.world, status=RunStatus.GENERATING, outline_state=OutlineState.APPROVED,
        sections=2, generating=1,
    )  # fmt: skip
    r.world.audit.fail = True  # the audit chain is the last write of the unit
    with pytest.raises(RuntimeError):
        await r.cancel.execute(ID, run.id, "x")
    assert r.world.runs[run.id].status is RunStatus.GENERATING
    assert r.world.artefacts[art.id].status is ArtefactStatus.GENERATING
    assert sorted(s.state.value for s in r.world.sections.values()) == ["generating", "pending"]
    assert r.world.events == [] and r.workflows.signals == []  # and no signal either


async def test_w5_reason_is_truncated_to_256_characters() -> None:
    r = WfRig()
    _, run, _ = seed_document(r.world)
    await r.cancel.execute(ID, run.id, "r" * 400)
    assert len(r.world.events[0].payload["reason"]) == 256


async def test_w5_re_resolves_scope_only_so_a_read_only_tenant_can_cancel() -> None:
    r = WfRig()
    _, run, _ = seed_document(r.world)
    read_only = replace(ID, feature_flags=frozenset({READ_ONLY_FLAG}))
    r.packs.down = True  # even an unreachable pack service cannot stop a cancel
    await r.cancel.execute(read_only, run.id, None)
    assert r.packs.calls == 0  # no eligibility resolution (ASYNC-4: a cancel starts no work)
    assert r.world.runs[run.id].status is RunStatus.CANCELLED


async def test_w5_signal_failure_after_commit_is_503_and_a_repeat_raises_it_again() -> None:
    r = WfRig()
    _, run, _ = seed_document(r.world)
    r.workflows.fail_signal = engine_down()
    await raises(r.cancel.execute(ID, run.id, "x"), ErrorCode.WORKFLOW_ENGINE_UNAVAILABLE)
    assert r.world.runs[run.id].status is RunStatus.CANCELLED
    r.workflows.fail_signal = None
    await r.cancel.execute(ID, run.id, "x")  # idempotent: the lost signal goes out now
    assert len(r.workflows.signals) == 1 and len(r.world.events) == 1


async def test_w5_a_finished_run_cannot_be_cancelled() -> None:
    r = WfRig()
    _, run, _ = seed_document(
        r.world, status=RunStatus.COMPLETE, outline_state=OutlineState.APPROVED
    )
    await raises(r.cancel.execute(ID, run.id, None), ErrorCode.WORKFLOW_STATE_CONFLICT)


async def test_w5_cancelling_a_regeneration_run_leaves_a_complete_artefact_complete() -> None:
    r = WfRig()
    art, run, _ = seed_document(
        r.world, status=RunStatus.PENDING, artefact_status=ArtefactStatus.COMPLETE,
        outline_state=OutlineState.NONE,
    )  # fmt: skip
    await r.cancel.execute(ID, run.id, None)
    assert r.world.artefacts[art.id].status is ArtefactStatus.COMPLETE  # nothing to cancel there
    assert r.world.runs[run.id].status is RunStatus.CANCELLED


async def test_w5_empty_scope_is_404_and_ends_the_run_scope_revoked() -> None:
    r = WfRig()
    _, run, _ = seed_document(r.world)
    r.scope.error = revoked()
    await raises(r.cancel.execute(ID, run.id, None), ErrorCode.ARTEFACT_NOT_FOUND)
    assert r.world.runs[run.id].failure_reason is RunFailureReason.SCOPE_REVOKED


# =============================== W-7 + the quota consumer =========================================
def suspended(r: WfRig, **kw: object):  # noqa: ANN202
    return seed_document(
        r.world, status=RunStatus.SUSPENDED, outline_state=OutlineState.APPROVED,
        failure_reason=RunFailureReason.QUOTA_EXHAUSTED, sections=3, complete=1, **kw,
    )  # fmt: skip


async def test_w7_resume_moves_the_run_clears_the_reason_and_signals() -> None:
    r = WfRig()
    _, run, _ = suspended(r)
    ack = await r.resume.execute(ID, run.id)
    after = r.world.runs[run.id]
    assert (after.status, after.failure_reason) == (RunStatus.GENERATING, None)
    assert ack.status is RunStatus.GENERATING
    assert r.world.events[0].payload["run_status"] == "generating"
    assert r.workflows.signals == [(run.workflow_id, "resume", None)]


async def test_w7_a_run_that_is_not_suspended_is_a_409() -> None:
    r = WfRig()
    _, run, _ = seed_document(
        r.world, status=RunStatus.GENERATING, outline_state=OutlineState.APPROVED
    )
    await raises(r.resume.execute(ID, run.id), ErrorCode.WORKFLOW_STATE_CONFLICT)


async def test_w7_re_resolves_scope_and_eligibility_before_acting() -> None:
    r = WfRig()
    _, run, _ = suspended(r)
    r.scope.error = revoked()
    await raises(r.resume.execute(ID, run.id), ErrorCode.ARTEFACT_NOT_FOUND)
    assert r.world.runs[run.id].failure_reason is RunFailureReason.SCOPE_REVOKED  # ASYNC-15
    r2 = WfRig(
        packs_kw={"default_eligibility": EligibilityDecl(EligibilityState.NOT_GENERATABLE, "no")}
    )
    _, run2, _ = suspended(r2)
    await raises(r2.resume.execute(ID, run2.id), ErrorCode.GENERATION_NOT_PERMITTED)
    assert r2.world.runs[run2.id].status is RunStatus.SUSPENDED and r2.workflows.signals == []


async def test_quota_restored_consumer_resumes_as_the_stored_principal() -> None:
    r = WfRig()
    _, run, _ = suspended(r)
    await r.resumer.resume(SuspendedRun(run.id, run.artefact_id, run.principal_id))
    assert r.directory.calls == [(TENANT, USER, "standard", "standard")]  # ASYNC-11: re-read
    assert r.scope.calls[-1][0].principal_id == USER  # scope re-resolved for the stored principal
    assert r.world.runs[run.id].status is RunStatus.GENERATING
    assert r.workflows.signals == [(run.workflow_id, "resume", None)]


async def test_quota_consumer_never_signals_a_run_that_is_not_suspended_on_quota() -> None:
    r = WfRig()
    _, run, _ = seed_document(
        r.world, status=RunStatus.GENERATING, outline_state=OutlineState.APPROVED
    )
    await r.resumer.resume(SuspendedRun(run.id, run.artefact_id, run.principal_id))
    _, other, _ = seed_document(
        r.world, status=RunStatus.SUSPENDED, outline_state=OutlineState.APPROVED,
        failure_reason=None, run_input={"plan": None},
    )  # fmt: skip
    await r.resumer.resume(SuspendedRun(other.id, other.artefact_id, other.principal_id))
    assert r.workflows.signals == [] and r.directory.calls == []  # ASYNC-8


async def test_quota_consumer_an_iam_outage_raises_and_never_resolves_an_empty_identity() -> None:
    r = WfRig()
    _, run, _ = suspended(r)
    r.directory.error = DomainError(ErrorCode.SCOPE_STORE_UNAVAILABLE)
    await raises(
        r.resumer.resume(SuspendedRun(run.id, run.artefact_id, run.principal_id)),
        ErrorCode.SCOPE_STORE_UNAVAILABLE,
    )
    assert r.scope.calls == []  # nothing was resolved against a guessed identity
    assert r.world.runs[run.id].status is RunStatus.SUSPENDED  # redelivery will retry


async def test_quota_consumer_an_empty_scope_ends_the_run_and_is_not_an_error() -> None:
    r = WfRig()
    _, run, _ = suspended(r)
    r.scope.error = revoked()
    await r.resumer.resume(SuspendedRun(run.id, run.artefact_id, run.principal_id))  # no raise
    assert r.world.runs[run.id].failure_reason is RunFailureReason.SCOPE_REVOKED


async def test_quota_consumer_engine_outage_propagates_so_the_message_retries() -> None:
    r = WfRig()
    _, run, _ = suspended(r)
    r.workflows.fail_signal = engine_down()
    await raises(
        r.resumer.resume(SuspendedRun(run.id, run.artefact_id, run.principal_id)),
        ErrorCode.WORKFLOW_ENGINE_UNAVAILABLE,
    )


async def test_the_events_slices_quota_use_case_now_resumes_through_the_real_resumer() -> None:
    r = WfRig()
    _, run, _ = suspended(r)

    class Directory:
        async def suspended_on_quota(self, tx, *, after, limit):  # noqa: ANN001, ANN202
            return (
                [SuspendedRun(run.id, run.artefact_id, run.principal_id)] if after is None else []
            )

    use_case = ResumeSuspendedRuns(r.tx, Directory(), r.resumer, batch_size=10)  # type: ignore[arg-type]
    assert await use_case.execute(resource="tokens") == 1
    assert r.world.runs[run.id].status is RunStatus.GENERATING
    r.workflows.available = False  # no engine: the use case refuses instead of reporting a resume
    await raises(use_case.execute(resource="tokens"), ErrorCode.WORKFLOW_ENGINE_UNAVAILABLE)


# =============================== W-8 ==============================================================
OPERATOR = replace(ID, roles=frozenset({"platform_operator"}))


async def test_w8_requires_platform_operator() -> None:
    r = WfRig()
    _, run, _ = seed_document(r.world)
    await raises(r.rematerialise.execute(ID, run.id, "OPS-1", None), ErrorCode.OPERATOR_REQUIRED)
    assert r.workflows.starts == [] and r.world.audit.entries == []


async def test_w8_restarts_under_the_same_id_from_the_run_input_and_audits_with_the_ticket() -> (
    None
):
    r = WfRig()
    art, run, _ = seed_document(r.world)
    out = await r.rematerialise.execute(OPERATOR, run.id, "OPS-1234", "restore of 2026-10-12")
    (start,) = r.workflows.starts
    assert (start.workflow_id, start.run_input, start.restart) == (
        run.workflow_id,
        run.run_input,
        True,
    )
    assert (out.generation_id, out.artefact_id, out.workflow_id) == (
        run.id,
        art.id,
        run.workflow_id,
    )
    (entry,) = r.world.audit.entries
    assert entry.draft.entry_type == audit.CROSS_TENANT_ACCESS[0]
    assert entry.draft.metadata["operation"] == "rematerialise"
    assert entry.draft.metadata["ticket_ref"] == "OPS-1234"
    assert entry.draft.metadata["artefact_id"] == str(art.id)
    assert entry.draft.actor_id == USER


async def test_w8_a_live_workflow_is_a_409_and_nothing_is_audited() -> None:
    r = WfRig()
    _, run, _ = seed_document(r.world)
    assert run.workflow_id is not None
    r.workflows.live.add(run.workflow_id)
    await raises(
        r.rematerialise.execute(OPERATOR, run.id, "OPS-1", None), ErrorCode.WORKFLOW_STATE_CONFLICT
    )
    assert r.world.audit.entries == []  # the route cannot start two workflows for one run


async def test_w8_repeat_while_the_new_workflow_is_live_is_a_409() -> None:
    r = WfRig()
    _, run, _ = seed_document(r.world)
    await r.rematerialise.execute(OPERATOR, run.id, "OPS-1", None)
    await raises(
        r.rematerialise.execute(OPERATOR, run.id, "OPS-1", None), ErrorCode.WORKFLOW_STATE_CONFLICT
    )
    assert len(r.world.audit.entries) == 1


@pytest.mark.parametrize("status", [RunStatus.GENERATING, RunStatus.COMPLETE, RunStatus.CANCELLED])
async def test_w8_only_suspended_or_awaiting_runs_are_restartable(status: RunStatus) -> None:
    r = WfRig()
    _, run, _ = seed_document(r.world, status=status, outline_state=OutlineState.APPROVED)
    await raises(
        r.rematerialise.execute(OPERATOR, run.id, "OPS-1", None), ErrorCode.WORKFLOW_STATE_CONFLICT
    )


async def test_w8_unknown_run_and_unreachable_engine() -> None:
    r = WfRig()
    await raises(
        r.rematerialise.execute(OPERATOR, uuid4(), "OPS-1", None), ErrorCode.ARTEFACT_NOT_FOUND
    )
    _, run, _ = seed_document(r.world)
    r.workflows.fail_start = engine_down()
    await raises(
        r.rematerialise.execute(OPERATOR, run.id, "OPS-1", None),
        ErrorCode.WORKFLOW_ENGINE_UNAVAILABLE,
    )
    assert r.world.audit.entries == []  # nothing started, nothing to record


async def test_make_run_helper_builds_a_valid_pending_run() -> None:
    r = WfRig()
    art, _, _ = seed_document(r.world)
    assert make_run(art).status is RunStatus.PENDING


# =============================== the events slice's sweep, over the real starter ==================
async def test_the_orphan_sweep_restarts_a_stuck_w1_run_and_fails_a_synchronous_orphan() -> None:
    from tests.usecase import test_sweeps as sw

    from rag.application.events.ports import WorkflowStarter  # noqa: F401 - the port it satisfies
    from rag.application.workflows.bridges import OrphanRestarter
    from rag.domain.generation.ops import StuckArtefact

    r = WfRig()
    r.workflows.fail_start = engine_down()
    await raises(r.start.execute(ID, cmd()), ErrorCode.WORKFLOW_ENGINE_UNAVAILABLE)
    (run,) = r.world.runs.values()  # ASYNC-2: a committed shell the engine never saw
    engine = FakeWorkflows()
    k = sw.kit(TENANT)
    repo = sw.Repo(k["scope"])
    repo.pending[TENANT] = [
        StuckArtefact(run.artefact_id, TENDER, run.id, USER, run.workflow_id, run.run_input),
        StuckArtefact(uuid4(), None, uuid4(), USER, None, {"synchronous": True}),
    ]
    sweep, events, _ = sw.orphan(OrphanRestarter(engine, r.queues), repo, k)  # type: ignore[arg-type]
    out = await sweep.run()
    (start,) = engine.starts
    assert (start.workflow_id, start.run_input, start.task_queue) == (
        run.workflow_id,
        run.run_input,
        "q",
    )
    assert (
        out.affected == 1 and len(repo.failed) == 1
    )  # the synchronous orphan has nothing to start
    assert repo.failed[0] != run.artefact_id and [e.type for e in events.events] == [
        "GenerationFailed"
    ]


async def test_w4_signal_failure_after_commit_is_503_and_a_repeat_raises_it_again() -> None:
    r = WfRig()
    _, run, _ = seed_document(r.world, outline_version=1)
    edit = OutlineEdit(1, (OutlineEntry("sec-1", "introduction"),))
    r.workflows.fail_signal = engine_down()
    await raises(r.revise.execute(ID, run.id, edit), ErrorCode.WORKFLOW_ENGINE_UNAVAILABLE)
    assert r.world.runs[run.id].outline_version == 2  # the revision committed
    r.workflows.fail_signal = None
    ack = await r.revise.execute(ID, run.id, edit)  # not a 409, and not version 3
    assert ack.outline_version == 2 and r.world.runs[run.id].outline_version == 2
    (sig,) = r.workflows.signals
    assert sig[2] == {"outline_version": 2, "sections": [{"ref": "sec-1", "type": "introduction"}]}
    assert len(r.world.events) == 1  # one progress event, one signal
