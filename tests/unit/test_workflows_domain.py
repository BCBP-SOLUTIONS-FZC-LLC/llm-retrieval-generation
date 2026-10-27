"""Workflows and regeneration domain: ids, run input, gate machine, REGEN-*, refiner decisions."""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
from tests.support_generation import make_artefact, make_section, make_section_version
from tests.support_workflows import make_run

from rag.domain.generation import facts as fact_rules
from rag.domain.generation import instruction as ir
from rag.domain.generation import regeneration as regen
from rag.domain.generation.exceptions import InvariantViolation
from rag.domain.generation.facts import DocumentFact, FactConflict
from rag.domain.packs.profile import PLATFORM_DEFAULT_BUNDLE
from rag.domain.shared.enums import (
    ArtefactKind,
    ArtefactStatus,
    ConflictResolution,
    InstructionType,
    OutlineState,
    RefinerOutcome,
    RunFailureReason,
    RunStage,
    RunStatus,
    SectionState,
)
from rag.domain.shared.errors import DomainError, ErrorCode
from rag.domain.workflows import gate
from rag.domain.workflows.ids import (
    original_workflow_id,
    regeneration_workflow_id,
    task_queue_for,
)
from rag.domain.workflows.input import ScopeSeed, WorkflowInput

NOW = datetime(2026, 4, 2, 9, 0, tzinfo=UTC)
T, U, A = uuid4(), uuid4(), uuid4()


# --- RUN-2: deterministic workflow ids ---------------------------------------------------------
def test_workflow_ids_are_deterministic_and_never_collide() -> None:
    art, base = uuid4(), uuid4()
    assert original_workflow_id(art) == str(art)
    assert regeneration_workflow_id(base) == f"regen:{base}"
    assert regeneration_workflow_id(base) == regeneration_workflow_id(base)  # a repeat is absorbed
    assert regeneration_workflow_id(art) != original_workflow_id(art)  # no collision with the run
    # that produced the artefact


@pytest.mark.parametrize(
    ("plan", "residency", "queue"),
    [
        (None, "standard", "q"),
        ("starter", "standard", "q"),
        ("Enterprise", "standard", "q-ent"),
        ("starter", "strict", "q-strict"),
        ("enterprise", "strict", "q-strict"),  # a strict tenant's queue is its own whatever it pays
    ],
)
def test_task_queue_follows_plan_and_residency(
    plan: str | None, residency: str, queue: str
) -> None:
    got = task_queue_for(plan, residency, default="q", enterprise="q-ent", strict="q-strict")
    assert got == queue


# --- GEN-11 / ASYNC-6 / ASYNC-12: the run input is identifiers and a seed ----------------------
def _input(**kw: object) -> WorkflowInput:
    base: dict[str, object] = dict(
        tenant_id=T,
        principal_id=U,
        artefact_id=A,
        generation_run_id=uuid4(),
        capability="technical_proposal",
        requested_mode="assistance",
        plan="enterprise",
        residency_mode="standard",
        domain_profile_version="dp-1",
        language_pack_version="lp-1",
        scope_seed=ScopeSeed(tender_id=uuid4()),
    )
    return WorkflowInput(**{**base, **kw})  # type: ignore[arg-type]


def test_run_input_carries_no_departments_roles_or_resolved_scope() -> None:
    body = _input().to_dict()
    assert "scope_seed" in body and "scope" not in body  # ASYNC-6: a seed, never a token
    assert not {"departments", "roles", "scope_token"} & set(body)  # ASYNC-11
    json.dumps(body)  # plain JSON: it is stored on the run and sent to the engine


def test_run_input_refuses_authority_and_content_by_another_name() -> None:
    for key in ("departments", "roles", "scope_token", "content", "prompt"):
        with pytest.raises(InvariantViolation):
            _input(extras={key: "x"})
    with pytest.raises(InvariantViolation):
        _input(extras={"schema_ref": "x" * 600})  # a long value is content by another name
    with pytest.raises(InvariantViolation):
        _input(origin="regeneration")  # a regeneration names its base version


def test_scope_seed_round_trips() -> None:
    seed = ScopeSeed(uuid4(), (uuid4(),), ("tender",))
    assert ScopeSeed.from_dict(seed.to_dict()) == seed


def test_regeneration_input_names_base_version_and_recipients() -> None:
    base = uuid4()
    body = _input(
        origin="regeneration",
        base_version_id=base,
        instruction="shorter",
        recipients=(("user", "5"),),
    ).to_dict()
    assert body["base_version_id"] == str(base) and body["recipients"] == [
        {"kind": "user", "id": "5"}
    ]


# --- the gate machine: W-3 / W-4 / W-5 / W-7, RUN-7 --------------------------------------------
def _gated(version: int = 1, **kw: object):  # noqa: ANN202
    art = make_artefact(status=ArtefactStatus.GENERATING)
    return make_run(
        art,
        status=RunStatus.AWAITING_OUTLINE_APPROVAL,
        outline_state=OutlineState.READY,
        outline_version=version,
        current_stage=RunStage.OUTLINE,
        **kw,  # type: ignore[arg-type]
    )


def test_approve_moves_run_and_outline_and_stage_together() -> None:
    run = _gated()
    gate.approve(run, 1)
    assert (run.status, run.outline_state, run.current_stage) == (
        RunStatus.GENERATING,
        OutlineState.APPROVED,
        RunStage.STAGE1,
    )


def test_revise_increments_outline_version_and_returns_to_the_outline_phase() -> None:
    run = _gated(version=2)
    gate.revise(run, 2)
    assert (run.status, run.outline_state, run.outline_version, run.current_stage) == (
        RunStatus.OUTLINE_BUILDING,
        OutlineState.REVISING,
        3,
        RunStage.OUTLINE,
    )


def test_the_w3_w4_loop_then_approve() -> None:
    """ready -> revising -> (activity: building -> ready) -> approved, version 1 -> 2."""
    run = _gated(version=1)
    gate.revise(run, 1)
    run.outline_started()  # the outline activity: revising -> building (not this service)
    run.outline_written(NOW)  # building -> ready, awaiting approval, version unchanged by it
    assert (run.status, run.outline_version) == (RunStatus.AWAITING_OUTLINE_APPROVAL, 2)
    with pytest.raises(DomainError) as stale:
        gate.approve(run, 1)  # the reviewer approving the outline they saw
    assert stale.value.code is ErrorCode.WORKFLOW_STATE_CONFLICT
    gate.approve(run, 2)
    assert run.outline_state is OutlineState.APPROVED


@pytest.mark.parametrize("action", [gate.approve, gate.revise])
@pytest.mark.parametrize(
    "status",
    [RunStatus.PENDING, RunStatus.OUTLINE_BUILDING, RunStatus.GENERATING, RunStatus.COMPLETE],
)
def test_gate_actions_refuse_a_run_not_awaiting_approval(action, status: RunStatus) -> None:  # noqa: ANN001
    run = replace(_gated(), status=status, outline_state=OutlineState.BUILDING)
    with pytest.raises(DomainError) as exc:
        action(run, 1)
    assert exc.value.code is ErrorCode.WORKFLOW_STATE_CONFLICT


def test_a_repeat_of_approve_is_recognised_so_a_lost_signal_can_be_raised_again() -> None:
    run = _gated()
    gate.approve(run, 1)
    assert gate.already_approved(run, 1) and not gate.already_approved(run, 2)


@pytest.mark.parametrize(
    "status",
    [
        RunStatus.PENDING,
        RunStatus.OUTLINE_BUILDING,
        RunStatus.AWAITING_OUTLINE_APPROVAL,
        RunStatus.GENERATING,
        RunStatus.SUSPENDED,
    ],
)
def test_cancel_from_any_non_terminal_status_and_never_from_a_terminal_one(
    status: RunStatus,
) -> None:
    run = replace(_gated(), status=status)
    gate.cancel(run)
    assert run.status is RunStatus.CANCELLED
    with pytest.raises(DomainError):
        gate.cancel(run)  # cancelled is terminal: the use case treats a repeat separately


def test_resume_only_a_suspended_run_and_it_clears_the_spent_reason() -> None:
    run = replace(
        _gated(), status=RunStatus.SUSPENDED, failure_reason=RunFailureReason.QUOTA_EXHAUSTED
    )
    assert gate.resumable_on_quota(run)
    gate.resume(run)
    assert (run.status, run.failure_reason) == (RunStatus.GENERATING, None)
    with pytest.raises(DomainError):
        gate.resume(run)  # no longer suspended


def test_resumable_on_quota_needs_the_quota_reason() -> None:
    other = replace(_gated(), status=RunStatus.SUSPENDED, failure_reason=None)
    assert not gate.resumable_on_quota(other)  # only quota_exhausted may be resumed by the consumer


@pytest.mark.parametrize("status", [RunStatus.SUSPENDED, RunStatus.AWAITING_OUTLINE_APPROVAL])
def test_rematerialise_only_runs_waiting_on_a_person(status: RunStatus) -> None:
    gate.require_restartable(replace(_gated(), status=status))
    for bad in (RunStatus.GENERATING, RunStatus.COMPLETE, RunStatus.FAILED, RunStatus.PENDING):
        run = replace(_gated(), status=bad, failure_reason=RunFailureReason.INTERNAL)
        with pytest.raises(DomainError):
            gate.require_restartable(run)


@pytest.mark.parametrize(
    "status",
    [
        RunStatus.PENDING,
        RunStatus.OUTLINE_BUILDING,
        RunStatus.AWAITING_OUTLINE_APPROVAL,
        RunStatus.GENERATING,
        RunStatus.SUSPENDED,
    ],
)
def test_scope_revoked_ends_any_live_run_with_its_reason_and_stage(status: RunStatus) -> None:
    run = replace(_gated(), status=status, current_stage=RunStage.STAGE2)
    run.revoke_scope()
    assert (run.status, run.failure_reason, run.failed_at_stage) == (
        RunStatus.FAILED,
        RunFailureReason.SCOPE_REVOKED,
        RunStage.STAGE2,
    )
    with pytest.raises(InvariantViolation):
        run.revoke_scope()  # terminal: nothing leaves it


# --- REGEN-2, REGEN-4, REGEN-5, REGEN-8 ---------------------------------------------------------
def test_document_base_must_be_the_current_version() -> None:
    cur = uuid4()
    art = make_artefact(current_version_id=cur)
    regen.guard_document_base(art, cur)
    with pytest.raises(DomainError) as exc:
        regen.guard_document_base(art, uuid4())
    assert exc.value.code is ErrorCode.OPTIMISTIC_LOCK_CONFLICT
    with pytest.raises(DomainError):
        regen.guard_document_base(make_artefact(), cur)  # an artefact with no version has no base


def test_section_base_is_the_section_version_or_a_document_version_still_holding_it() -> None:
    doc = uuid4()
    art = make_artefact(current_version_id=doc)
    sec = make_section(art.id)
    sv = make_section_version(sec.id, uuid4())
    sec = replace(sec, state=SectionState.COMPLETE, current_version_id=sv.id)
    regen.guard_section_base(art, sec, sv.id, document_holds_current_section_version=False)
    regen.guard_section_base(art, sec, doc, document_holds_current_section_version=True)
    for base, holds in ((doc, False), (uuid4(), True)):  # a superseded reading, a stranger
        with pytest.raises(DomainError) as exc:
            regen.guard_section_base(art, sec, base, document_holds_current_section_version=holds)
        assert exc.value.code is ErrorCode.OPTIMISTIC_LOCK_CONFLICT


def test_a_section_with_no_version_can_only_be_based_on_the_current_document_version() -> None:
    doc = uuid4()
    art = make_artefact(current_version_id=doc)
    sec = make_section(art.id)
    regen.guard_section_base(art, sec, doc, document_holds_current_section_version=False)
    with pytest.raises(DomainError):
        regen.guard_section_base(art, sec, uuid4(), document_holds_current_section_version=False)


@pytest.mark.parametrize("status", [ArtefactStatus.PENDING, ArtefactStatus.GENERATING])
def test_a_run_in_flight_owns_the_artefact(status: ArtefactStatus) -> None:
    with pytest.raises(DomainError) as exc:
        regen.require_regenerable(make_artefact(status=status))
    assert exc.value.code is ErrorCode.OPTIMISTIC_LOCK_CONFLICT  # the 409 §24 gives A-5 / A-6


@pytest.mark.parametrize(
    "status", [ArtefactStatus.COMPLETE, ArtefactStatus.FAILED, ArtefactStatus.CANCELLED]
)
def test_an_artefact_at_rest_may_be_regenerated(status: ArtefactStatus) -> None:
    regen.require_regenerable(make_artefact(status=status))


def test_regeneration_scope_mints_a_document_version_only_at_document_scope() -> None:
    assert regen.mints_document_version(regen.RegenerationScope.DOCUMENT)  # REGEN-8: A-5
    assert not regen.mints_document_version(regen.RegenerationScope.SECTION)  # A-6


def test_fact_conflicts_exist_only_on_a_long_form_artefact_with_facts() -> None:
    doc = make_artefact(kind=ArtefactKind.DOCUMENT)
    draft = make_artefact(kind=ArtefactKind.SECTION_DRAFT)
    assert regen.fact_conflict_possible(doc, 2) and not regen.fact_conflict_possible(doc, 0)
    assert not regen.fact_conflict_possible(draft, 5)  # REGEN-4: a draft has no stage 1


def test_style_reuse_requires_the_scope_to_still_cover_the_set() -> None:
    a, b = uuid4(), uuid4()
    assert regen.scope_covers([a], [a, b]) and not regen.scope_covers([a, b], [a])  # REGEN-5


# --- the Instruction Refiner's decisions (REGEN-9, IR-1, REF-7) ----------------------------------
@pytest.mark.parametrize(
    ("kind", "model", "expected"),
    [
        (InstructionType.STYLE, True, False),  # style-only reuses the prior set
        (InstructionType.ADD, False, True),
        (InstructionType.CORRECT, False, True),
        (InstructionType.EXPAND, False, True),
        (InstructionType.REMOVE, False, False),  # the LLD is silent: the model decides
        (InstructionType.REMOVE, True, True),
    ],
)
def test_needs_retrieval_follows_the_instruction_type(
    kind: InstructionType, model: bool, expected: bool
) -> None:
    assert ir.needs_retrieval_for(kind, model) is expected


def _fact(key: str, value: str, section: UUID | None = None, **kw: object) -> DocumentFact:
    return DocumentFact(uuid4(), A, T, key, value, section or uuid4(), **kw)  # type: ignore[arg-type]


def test_a_conflict_is_believed_only_where_the_stored_fact_differs() -> None:
    crane = _fact("access_method", "crane")
    claim = {"fact_key": "access_method", "instruction_value": "hoist"}
    found = ir.validate_conflict(claim, [crane])
    assert found is not None and found.established_value == "crane"
    assert found.instruction_value == "hoist" and found.established_by_section_id == (
        crane.established_by_section_id
    )
    assert ir.validate_conflict({**claim, "instruction_value": " CRANE "}, [crane]) is None
    assert ir.validate_conflict({"fact_key": "invented", "instruction_value": "x"}, [crane]) is None
    assert ir.validate_conflict(claim, []) is None  # REGEN-4: no facts, nothing to conflict with
    old = _fact("access_method", "ladder", superseded_at=NOW)
    assert ir.validate_conflict(claim, [old]) is None  # a superseded fact is no longer established
    assert ir.validate_conflict(None, [crane]) is None
    assert ir.validate_conflict({"fact_key": 3, "instruction_value": "x"}, [crane]) is None


def test_parse_applies_the_type_rule_over_the_models_flag() -> None:
    body = {
        "instruction_type": "style",
        "refined_instruction": " Tighter. ",
        "needs_retrieval": True,
    }
    got = ir.parse("tighten", json.dumps(body), "ir-1", [])
    assert (got.needs_retrieval, got.refined_instruction, got.usable) == (False, "Tighter.", True)
    body = {"instruction_type": "add", "refined_instruction": "cite it", "needs_retrieval": False}
    assert ir.parse("x", json.dumps(body), "ir-1", []).needs_retrieval is True


@pytest.mark.parametrize(
    "bad",
    [
        "not json",
        json.dumps({"instruction_type": "style"}),
        json.dumps({"instruction_type": "rewrite", "refined_instruction": "x"}),
        json.dumps({"instruction_type": "style", "refined_instruction": "  "}),
    ],
)
def test_an_unusable_answer_raises_so_the_caller_degrades(bad: str) -> None:
    with pytest.raises((ValueError, KeyError)):
        ir.parse("x", bad, "ir-1", [])


def test_a_degraded_result_keeps_the_raw_instruction_and_always_searches() -> None:
    got = ir.degraded("tighten the intro", "ir-1", "deadline_exceeded")
    assert got.refined_instruction == got.raw_instruction == "tighten the intro"
    assert got.needs_retrieval is True  # IR-1: a degraded refine never skips a search
    assert (got.outcome, got.usable, got.marker_reason) == (
        RefinerOutcome.DEGRADED,
        False,
        "deadline_exceeded",
    )


def test_refiner_version_changes_with_the_pack_and_is_stable_otherwise() -> None:
    assert ir.refiner_version("dp-1") == ir.refiner_version("dp-1")
    assert ir.refiner_version("dp-1") != ir.refiner_version("dp-2")  # REF-7
    assert ir.refiner_version("dp-1").startswith("ir-")
    assert PLATFORM_DEFAULT_BUNDLE.domain_profile.version  # (the bundle the refiner pins)


# --- A-7: resolving a conflict (REGEN-6, LD-32) --------------------------------------------------
def _conflict(**kw: object) -> FactConflict:
    return FactConflict(
        uuid4(),
        T,
        A,
        uuid4(),
        "access_method",
        "crane",
        "hoist",
        uuid4(),
        **kw,  # type: ignore[arg-type]
    )


@pytest.mark.parametrize(
    ("resolution", "supersede", "flag", "proceeds"),
    [
        (ConflictResolution.UPDATE_FACT, True, True, True),
        (ConflictResolution.KEEP_ESTABLISHED, False, False, True),
        (ConflictResolution.DISMISS, False, False, False),  # dismiss ends the interaction (LD-32)
    ],
)
def test_resolutions_and_what_they_entail(
    resolution: ConflictResolution, supersede: bool, flag: bool, proceeds: bool
) -> None:
    c = _conflict()
    out = fact_rules.resolve_conflict(c, resolution, NOW)
    assert (out.supersede_fact, out.flag_sections, out.proceeds) == (supersede, flag, proceeds)
    assert out.insert_value == ("hoist" if supersede else None)
    assert (c.resolution, c.resolved_at) == (resolution, NOW)


def test_a_resolved_conflict_is_a_409_carrying_the_recorded_resolution() -> None:
    c = _conflict(resolution=ConflictResolution.DISMISS, resolved_at=NOW)
    with pytest.raises(DomainError) as exc:
        fact_rules.resolve_conflict(c, ConflictResolution.UPDATE_FACT, NOW)
    assert exc.value.code is ErrorCode.WORKFLOW_STATE_CONFLICT
    assert exc.value.extensions == {"resolution": "dismiss"}
