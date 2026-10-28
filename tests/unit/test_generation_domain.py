"""Generation entities: invariants, state machines, tier roll-up, audit hashing, pagination."""

from __future__ import annotations

import hashlib
import re
from dataclasses import replace
from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal
from itertools import product
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from tests.support_generation import (
    NOW,
    TENANT,
    USER,
    make_artefact,
    make_section,
    make_section_version,
    make_version,
)

from rag.application.shared.pagination import MAX_LIMIT, clamp_window
from rag.domain.generation import audit
from rag.domain.generation.artefact import ARTEFACT_TRANSITIONS
from rag.domain.generation.audit import AuditEntry, AuditEntryDraft
from rag.domain.generation.evidence import roll_up_evidence_tier
from rag.domain.generation.exceptions import InvariantViolation
from rag.domain.generation.facts import FactConflict
from rag.domain.generation.retrieval import RetrievalSet, RetrievalSetPurge
from rag.domain.generation.run import OUTLINE_TRANSITIONS, RUN_TRANSITIONS, GenerationRun
from rag.domain.generation.section import SECTION_TRANSITIONS
from rag.domain.generation.verification import ClaimVerification, GroundednessReport
from rag.domain.shared.enums import (
    ArtefactKind,
    ArtefactOrigin,
    ArtefactStatus,
    AuditActorType,
    ClaimOutcome,
    ConflictResolution,
    EligibilityState,
    EvidenceTier,
    GenerationMode,
    GrantLevel,
    OutlineState,
    RetrievalVerdict,
    RunFailureReason,
    RunStage,
    RunStatus,
    SectionState,
)
from rag.domain.shared.errors import DomainError, ErrorCode

MIGRATION = (
    Path(__file__).resolve().parents[2] / "migrations/generation/0002_generation_tables.up.sql"
)

# --- artefact ----------------------------------------------------------------------------


def test_art1_tender_less_only_for_answers() -> None:
    """ART-1: a free-standing artefact must be an answer."""
    make_artefact(tender_id=None, kind=ArtefactKind.ANSWER)
    with pytest.raises(InvariantViolation, match="ART-1"):
        make_artefact(tender_id=None, kind=ArtefactKind.DOCUMENT)


def test_art5_status_table_and_superseded_is_reserved() -> None:
    """ART-5: the table, and nothing may write or leave ``superseded``."""
    a = make_artefact()
    a.move_to(ArtefactStatus.GENERATING)
    a.move_to(ArtefactStatus.COMPLETE)
    a.move_to(ArtefactStatus.GENERATING)  # regeneration re-enters
    for target in ArtefactStatus:
        assert ArtefactStatus.SUPERSEDED not in ARTEFACT_TRANSITIONS[target]
    assert ARTEFACT_TRANSITIONS[ArtefactStatus.SUPERSEDED] == frozenset()
    with pytest.raises(InvariantViolation, match="ART-5"):
        make_artefact().move_to(ArtefactStatus.COMPLETE)  # pending -> complete


def test_art7_archive_and_tender_closed() -> None:
    """ART-7/8: archiving needs a tender; an archived artefact refuses new work."""
    a = make_artefact()
    a.require_writable()
    a.archive(NOW)
    with pytest.raises(DomainError) as err:
        a.require_writable()
    assert err.value.code is ErrorCode.TENDER_CLOSED
    with pytest.raises(InvariantViolation, match="ART-8"):
        make_artefact(tender_id=None, kind=ArtefactKind.ANSWER).archive(NOW)


def test_av2_clamp_needs_reason() -> None:
    """AV-2: a clamp is always recorded with its reason."""
    aid = uuid4()
    make_version(aid, clamped=True, clamp_reason="profile_withholds_unattended_completion")
    with pytest.raises(InvariantViolation, match="AV-2"):
        make_version(aid, clamped=True)


# --- sections ----------------------------------------------------------------------------


def test_section_table_matches_the_trigger_function() -> None:
    """Every (old, new) pair the Python table allows is one ``reject_section_transition`` allows."""
    sql = MIGRATION.read_text()
    body = sql[sql.index("CREATE FUNCTION reject_section_transition") :].split("$$;")[0]
    for predicate in (
        "OLD.state = 'pending'    AND NEW.state IN ('generating', 'gap')",
        "OLD.state = 'generating' AND NEW.state IN ('complete', 'gap', 'failed')",
        "OLD.state = 'complete'   AND NEW.state = 'generating'",
        "OLD.state IN ('gap', 'failed') AND NEW.state = 'generating'",
    ):
        assert predicate in body
    expected = {
        ("pending", "generating"),
        ("pending", "gap"),
        ("generating", "complete"),
        ("generating", "gap"),
        ("generating", "failed"),
        ("complete", "generating"),
        ("gap", "generating"),
        ("failed", "generating"),
    }
    actual = {
        (o.value, n.value)
        for o, n in product(SectionState, SectionState)
        if n in SECTION_TRANSITIONS[o]
    }
    assert actual == expected


def test_as5_gap_failed_reasons_clear_on_exit() -> None:
    """AS-5: leaving gap or failed clears the reason that required it."""
    aid = uuid4()
    gap = make_section(aid)
    gap.block("required_input_missing")
    assert (gap.state, gap.gap_reason) == (SectionState.GAP, "required_input_missing")
    gap.start()
    assert gap.gap_reason is None
    failed = make_section(aid)
    failed.start()
    failed.end_without_version(gap_reason=None, failure=RunFailureReason.INFERENCE_UNAVAILABLE)
    assert failed.failure_reason is RunFailureReason.INFERENCE_UNAVAILABLE
    failed.start()
    assert failed.failure_reason is None


def test_as7_section_with_a_version_is_never_gap_or_failed() -> None:
    """AS-7: a regeneration that ends without a version returns to complete."""
    sec = make_section(uuid4(), current_version_id=uuid4(), state=SectionState.COMPLETE)
    previous = sec.current_version_id
    sec.start()
    sec.end_without_version(gap_reason="insufficient_twice", failure=RunFailureReason.INTERNAL)
    assert (sec.state, sec.current_version_id) == (SectionState.COMPLETE, previous)
    with pytest.raises(InvariantViolation, match="AS-7"):
        make_section(
            uuid4(),
            state=SectionState.FAILED,
            failure_reason=RunFailureReason.INTERNAL,
            current_version_id=uuid4(),
        )


def test_as8_run_end_closes_only_generating_sections() -> None:
    """AS-8: generating -> failed (no version) / complete (version); pending stays."""
    aid = uuid4()
    no_version, with_version, pending = (
        make_section(aid, 1),
        make_section(aid, 2),
        make_section(aid, 3),
    )
    with_version.current_version_id = uuid4()
    for s in (no_version, with_version):
        s.start()
    for s in (no_version, with_version, pending):
        s.close_in_flight(RunFailureReason.INTERNAL)
    assert no_version.state is SectionState.FAILED
    assert with_version.state is SectionState.COMPLETE
    assert pending.state is SectionState.PENDING


def test_as2_gap_needs_reason_and_illegal_moves_refused() -> None:
    """AS-2 and an unlisted pair."""
    with pytest.raises(InvariantViolation, match="AS-2"):
        make_section(uuid4(), state=SectionState.GAP)
    with pytest.raises(InvariantViolation, match="AS-5"):
        make_section(uuid4()).complete()  # pending -> complete is not an edge


def test_asv_retry_cap_and_digest_length() -> None:
    """ASV-2 caps auto retries at two; ASV-4 needs a sha256."""
    rs = uuid4()
    make_section_version(uuid4(), rs, auto_retry_count=2)
    with pytest.raises(InvariantViolation, match="ASV-2"):
        make_section_version(uuid4(), rs, auto_retry_count=3)
    with pytest.raises(InvariantViolation, match="ASV-4"):
        make_section_version(uuid4(), rs, content_sha256=b"x")


# --- runs --------------------------------------------------------------------------------


def make_run(**kw: object) -> GenerationRun:
    base: dict[str, object] = dict(
        id=uuid4(),
        artefact_id=uuid4(),
        tenant_id=TENANT,
        principal_id=USER,
        requested_mode=GenerationMode.ASSISTANCE,
        effective_mode=GenerationMode.ASSISTANCE,
        eligibility_state=EligibilityState.GENERATABLE,
        domain_profile_version="dp-1",
        language_pack_version="lp-1",
    )
    return GenerationRun(**{**base, **kw})  # type: ignore[arg-type]


def test_run_terminal_states_have_no_exit() -> None:
    """complete, failed and cancelled are terminal; every other state can be cancelled."""
    for terminal in (RunStatus.COMPLETE, RunStatus.FAILED, RunStatus.CANCELLED):
        assert RUN_TRANSITIONS[terminal] == frozenset()
    for status in RunStatus:
        if RUN_TRANSITIONS[status]:
            assert RunStatus.CANCELLED in RUN_TRANSITIONS[status]
    assert RunStatus.COMPLETE not in RUN_TRANSITIONS[RunStatus.SUSPENDED]


def test_run_gate_loop_w3_w4() -> None:
    """Outline gate: none -> building -> ready -> revising -> building -> ready -> approved."""
    run = make_run()
    run.outline_started()
    assert (run.status, run.outline_state, run.outline_version) == (
        RunStatus.OUTLINE_BUILDING,
        OutlineState.BUILDING,
        1,
    )
    run.outline_written(NOW)
    assert run.status is RunStatus.AWAITING_OUTLINE_APPROVAL
    run.revise_outline()  # W-4
    assert (run.status, run.outline_state, run.outline_version) == (
        RunStatus.OUTLINE_BUILDING,
        OutlineState.REVISING,
        2,
    )
    assert run.current_stage is RunStage.OUTLINE
    run.outline_started()  # activity: revising -> building, run status already moved by W-4
    run.outline_written(NOW)
    run.approve_outline()  # W-3
    assert (run.status, run.outline_state, run.current_stage) == (
        RunStatus.GENERATING,
        OutlineState.APPROVED,
        RunStage.STAGE1,
    )
    assert OUTLINE_TRANSITIONS[OutlineState.APPROVED] == frozenset()


def test_run_cannot_skip_the_gate_or_leave_terminal() -> None:
    """RUN-7: only listed edges."""
    run = make_run()
    with pytest.raises(InvariantViolation, match="RUN-7"):
        run.move_to(RunStatus.COMPLETE)
    run.move_to(RunStatus.CANCELLED)
    assert run.is_terminal
    with pytest.raises(InvariantViolation, match="RUN-7"):
        run.move_to(RunStatus.GENERATING)


def test_run_failure_carries_reason_and_invariants() -> None:
    """RUN-5, RUN-1 clamp and base version, RUN-3 key, ck_run_outline."""
    run = make_run()
    run.move_to(RunStatus.GENERATING)
    run.fail(RunFailureReason.QUOTA_EXHAUSTED, RunStage.STAGE2)
    assert (run.failure_reason, run.failed_at_stage) == (
        RunFailureReason.QUOTA_EXHAUSTED,
        RunStage.STAGE2,
    )
    with pytest.raises(InvariantViolation, match="RUN-5"):
        make_run(status=RunStatus.FAILED)
    with pytest.raises(InvariantViolation, match="RUN-1"):
        make_run(clamped=True)
    with pytest.raises(InvariantViolation, match="RUN-1"):
        make_run(origin=ArtefactOrigin.REGENERATION)
    with pytest.raises(InvariantViolation, match="RUN-3"):
        make_run(idempotency_key="k")
    with pytest.raises(InvariantViolation, match="RUN-7"):
        make_run(outline_state=OutlineState.READY)


# --- retrieval, verification, facts -------------------------------------------------------


def test_rs5_and_cv3_and_gr_totals_and_fc() -> None:
    """RS-5, CV-3, ck_gr_totals, VER-5 and the fact-conflict pair rule."""
    base = dict(
        id=uuid4(),
        tenant_id=TENANT,
        scope_resolution_hash="h",
        grant_level_applied=GrantLevel.SEARCH,
        filter_set={},
        index_generations={},
        profile_source="x",
        applied_profile={},
        reranker_ran=False,
        budget_allocation={},
        domain_profile_version="d",
        language_pack_version="l",
    )
    RetrievalSet(verdict=RetrievalVerdict.SUFFICIENT, **base)  # type: ignore[arg-type]
    with pytest.raises(InvariantViolation, match="RS-5"):
        RetrievalSet(verdict=RetrievalVerdict.INSUFFICIENT, **base)  # type: ignore[arg-type]
    with pytest.raises(InvariantViolation, match="CV-3"):
        ClaimVerification(uuid4(), 1, TENANT, ClaimOutcome.SUPPORTED, ("a:1",), 1, ("b:2",))
    with pytest.raises(InvariantViolation, match="GR"):
        GroundednessReport(
            uuid4(), uuid4(), TENANT, (1,), 5, 1, 1, 1, 1, EvidenceTier.PARTIAL, Decimal("0.5")
        )
    with pytest.raises(InvariantViolation, match="VER-5"):
        GroundednessReport(
            uuid4(), uuid4(), TENANT, (1,), 0, 0, 0, 0, 0, EvidenceTier.PARTIAL, None
        )
    with pytest.raises(InvariantViolation, match="FC"):
        FactConflict(
            uuid4(),
            TENANT,
            uuid4(),
            uuid4(),
            "k",
            "a",
            "b",
            uuid4(),
            resolution=ConflictResolution.DISMISS,
        )


def test_purge_marker_is_a_separate_value() -> None:
    """RSP-2: chunk ids are text, held beside the set."""
    p = RetrievalSetPurge(uuid4(), uuid4(), TENANT, uuid4(), 3, ("b7e2:1",), uuid4())
    assert p.chunk_ids == ("b7e2:1",)


# --- evidence roll-up ---------------------------------------------------------------------


@pytest.mark.parametrize(
    ("tiers", "expected"),
    [
        ([EvidenceTier.SUPPORTED, EvidenceTier.PARTIAL], EvidenceTier.PARTIAL),
        (
            [EvidenceTier.SUPPORTED, EvidenceTier.NOT_COMPUTED, EvidenceTier.PARTIAL],
            EvidenceTier.NOT_COMPUTED,
        ),
        ([EvidenceTier.SUPPORTED, EvidenceTier.INSUFFICIENT], EvidenceTier.INSUFFICIENT),
        ([EvidenceTier.SUPPORTED], EvidenceTier.SUPPORTED),
        ([], EvidenceTier.NOT_COMPUTED),
    ],
)
def test_md30_rollup_is_min_and_uncomputed_propagates(
    tiers: list[EvidenceTier], expected: EvidenceTier
) -> None:
    """MD-30, AOB-2: lowest wins; NOT_COMPUTED lowest; an empty set never reads as passing."""
    assert roll_up_evidence_tier(tiers) is expected


def test_rollup_is_order_independent() -> None:
    """AOB-3: arithmetic over recorded verdicts."""
    tiers = [EvidenceTier.PARTIAL, EvidenceTier.SUPPORTED, EvidenceTier.INSUFFICIENT]
    assert roll_up_evidence_tier(tiers) is roll_up_evidence_tier(reversed(tiers))


# --- audit hashing ------------------------------------------------------------------------


def draft(**kw: object) -> AuditEntryDraft:
    base: dict[str, object] = dict(
        tenant_id=TENANT,
        occurred_at=datetime(2026, 4, 2, 9, 14, 0, 123456, tzinfo=UTC),
        entry_type=audit.ARTEFACT_PURGED[0],
        action=audit.ARTEFACT_PURGED[1],
        actor_type=AuditActorType.SERVICE_ACCOUNT,
        actor_id=USER,
        artefact_id=UUID("0a3f0000-0000-7000-8000-0000000000b1"),
        source_event_type="llm.artefact.purged",
        source_event_id="evt-1",
        metadata={"reason": "tender_purged", "n": 2},
    )
    return AuditEntryDraft(**{**base, **kw})  # type: ignore[arg-type]


def test_canonical_json_sorted_no_whitespace_utf8() -> None:
    """AC-2: sorted keys, no whitespace, non-ASCII kept."""
    assert audit.canonical_json({"b": 1, "a": {"z": [1, "é"], "y": None}}) == (
        '{"a":{"y":null,"z":[1,"é"]},"b":1}'
    )


def test_payload_document_columns_and_utc_time() -> None:
    """The hashed object has every column above payload_hash; time is UTC microseconds."""
    d = draft(occurred_at=datetime(2026, 4, 2, 11, 14, 0, 5, tzinfo=timezone(timedelta(hours=2))))
    doc = audit.payload_document(d, 7)
    assert doc["occurred_at"] == "2026-04-02T09:14:00.000005Z"
    assert set(doc) == {
        "tenant_id", "seq", "occurred_at", "entry_type", "action", "actor_type", "actor_id",
        "artefact_id", "target_type", "target_id", "source_service", "source_event_type",
        "source_event_id", "trace_id", "metadata",
    }  # fmt: skip
    # the SQL trigger builds exactly this key set
    sql = MIGRATION.read_text()
    assert set(re.findall(r"^\s+'(\w+)', (?:NEW\.|to_char)", sql, re.M)) >= set(doc)


def test_hash_chain_links_and_detects_each_kind_of_break() -> None:
    """AC-2..AC-4: hashes recompute; tamper, gap and relink are all first breaks."""
    entries: list[AuditEntry] = []
    for seq in (1, 2, 3):
        d = draft(source_event_id=f"evt-{seq}")
        payload = audit.compute_payload_hash(d, seq)
        prev = entries[-1].entry_hash if entries else None
        entries.append(AuditEntry(d, seq, payload, prev, audit.compute_entry_hash(prev, payload)))
    assert entries[0].prev_hash is None
    assert audit.first_break(entries) is None
    tampered = replace(entries[1], draft=draft(source_event_id="evt-2", metadata={"reason": "x"}))
    assert audit.first_break([entries[0], tampered, entries[2]]) == 2
    assert audit.first_break([entries[0], entries[2]]) == 3  # gap: seq 3 sits at position 2
    relinked = replace(entries[2], prev_hash=entries[0].entry_hash)
    assert audit.first_break([entries[0], entries[1], relinked]) == 3


def test_entry_hash_of_first_entry_is_sha256_of_payload() -> None:
    """coalesce(prev_hash, '') || payload_hash at seq 1."""
    p = audit.compute_payload_hash(draft(), 1)
    assert audit.compute_entry_hash(None, p) == hashlib.sha256(p).digest()


def test_draft_rules_ac_actor_metadata_size() -> None:
    """chk_audit_actor, tz-aware time, and the 8 KiB metadata bound (AC-7)."""
    with pytest.raises(InvariantViolation):
        draft(actor_type=AuditActorType.ANONYMOUS)  # anonymous with an actor id
    with pytest.raises(InvariantViolation):
        draft(actor_type=AuditActorType.USER, actor_id=None)
    with pytest.raises(InvariantViolation):
        draft(occurred_at=datetime(2026, 1, 1))
    with pytest.raises(InvariantViolation, match="AC-7"):
        draft(metadata={"blob": "x" * 9000})
    draft(actor_type=AuditActorType.ANONYMOUS, actor_id=None)


def test_content_digest_is_order_independent_and_pairs_ids_with_hashes() -> None:
    """AC-8: sorted by section_version_id; one changed byte changes the digest."""
    a, b = UUID(int=1), UUID(int=2)
    ha, hb = hashlib.sha256(b"a").digest(), hashlib.sha256(b"b").digest()
    d1 = audit.content_digest([(a, ha), (b, hb)])
    assert d1 == audit.content_digest([(b, hb), (a, ha)])
    assert d1 == hashlib.sha256(f"{a}:{ha.hex()}\n{b}:{hb.hex()}".encode()).hexdigest()
    assert d1 != audit.content_digest([(a, ha), (b, hashlib.sha256(b"c").digest())])


# --- pagination ---------------------------------------------------------------------------


def test_pagination_defaults_clamp_and_echo() -> None:
    """5.1: default 100, ceiling 500 clamped (not rejected), offset default 0."""
    assert (clamp_window().limit, clamp_window().offset) == (100, 0)
    assert clamp_window(10_000, 5) == clamp_window(MAX_LIMIT, 5)
    assert clamp_window(1, 0).limit == 1


@pytest.mark.parametrize(("limit", "offset"), [(0, None), (-1, None), (None, -1)])
def test_pagination_backstop_rejects_non_positive(limit: int | None, offset: int | None) -> None:
    """The presentation layer returns 400; the helper refuses too."""
    with pytest.raises(ValueError):
        clamp_window(limit, offset)


# --- migration shape ----------------------------------------------------------------------

TABLES = [
    "artefacts", "artefact_versions", "artefact_version_sections", "artefact_sections",
    "artefact_section_versions", "generation_runs", "retrieval_sets", "retrieval_set_chunks",
    "retrieval_set_purges", "groundedness_reports", "claim_verifications", "completeness_checks",
    "document_facts", "fact_conflicts", "graph_checkpoints", "query_refinements",
    "instruction_refinements", "artefact_audit_chain", "processed_events",
]  # fmt: skip


def test_migration_creates_our_tables_and_neither_of_eventcommons() -> None:
    """18 tables here; eventcommon owns the outbox (OBX-4) and the inbox (PE-1)."""
    sql = MIGRATION.read_text()
    created = re.findall(r"^CREATE TABLE (\w+)", sql, re.M)
    assert sorted(created) == sorted(set(TABLES) - {"processed_events"})
    assert not [t for t in created if t.startswith("outbox") or t == "processed_events"]


def test_migration_rls_covers_every_table_of_ours() -> None:
    """RLS-1: every table this migration creates (the library's inbox and outbox are not ours)."""
    sql = MIGRATION.read_text()
    block = sql[sql.index("FOREACH t IN ARRAY ARRAY[") : sql.index("LOOP")]
    covered = set(re.findall(r"'(\w+)'", block))
    assert covered == set(TABLES) - {"processed_events"}  # the ledger is created elsewhere
    assert "FORCE ROW LEVEL SECURITY" in sql
    assert "tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid" in sql


def test_migration_deferrable_constraints_as_specified() -> None:
    """ART-2, AS-3 INITIALLY DEFERRED; ART-9's two cycle FKs INITIALLY IMMEDIATE."""
    sql = MIGRATION.read_text()
    assert len(re.findall(r"DEFERRABLE INITIALLY DEFERRED", sql)) == 2
    assert len(re.findall(r"DEFERRABLE INITIALLY IMMEDIATE", sql)) == 2


def test_migration_has_the_listed_triggers_and_grants() -> None:
    """The 4.8.2 triggers that belong to this schema, and the 4.1 grant shape."""
    sql = MIGRATION.read_text()
    for name in (
        "trg_version_immutable", "trg_audit_append_only", "trg_audit_chain_append",
        "trg_section_transition",
    ):  # fmt: skip
        assert f"CREATE TRIGGER {name}" in sql
    assert sql.count("CREATE TRIGGER trg_touch_updated_at") == 4
    assert sql.count("CREATE TRIGGER trg_bump_record_version") == 4
    assert "REVOKE INSERT, UPDATE ON graph_checkpoints FROM rag_app" in sql
    audit_delete = re.search(r"GRANT DELETE ON(.*?)TO rag_app", sql, re.S)
    assert audit_delete and "artefact_audit_chain" not in audit_delete.group(1)
    assert "processed_events" not in audit_delete.group(1)
