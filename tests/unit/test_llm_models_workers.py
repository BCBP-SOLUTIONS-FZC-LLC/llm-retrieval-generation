"""``llm_models.workers``: the boundary types decode exactly what the API stores and sends.

The API stores ``WorkflowInput.to_dict()`` in ``generation_runs.run_input`` and sends the
``for_wire`` form to Temporal. The Worker Pool decodes both with ``GenerationWorkflowInput``, so a
drift on either side fails here.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path
from uuid import UUID, uuid4

import pytest

from llm_models import workers as w
from rag.application.workflows import ports as api_ports
from rag.domain.workflows.input import ScopeSeed, WorkflowInput, for_wire

T = UUID("0a3f0000-0000-7000-8000-000000000001")
U = UUID("0a3f0000-0000-7000-8000-000000000002")
A = UUID("0a3f0000-0000-7000-8000-0000000000a1")
TENDER = UUID("0a3f0000-0000-7000-8000-0000000000b1")
BASE = UUID("0a3f0000-0000-7000-8000-0000000000c1")
COLLECTION = UUID("0a3f0000-0000-7000-8000-0000000000d1")


def made(**kw: object) -> WorkflowInput:
    base: dict[str, object] = dict(
        tenant_id=T,
        principal_id=U,
        artefact_id=A,
        generation_run_id=uuid4(),
        capability="technical_proposal",
        requested_mode="assistance",
        plan="standard",
        residency_mode="standard",
        domain_profile_version="dp-1",
        language_pack_version="lp-1",
        scope_seed=ScopeSeed(TENDER, (COLLECTION,), ("tender",)),
        tender_id=TENDER,
        trace_id="trace-1",
        correlation_id="corr-1",
    )
    return WorkflowInput(**{**base, **kw})  # type: ignore[arg-type]


def test_w1_decodes_from_the_stored_and_from_the_wire_form_alike() -> None:
    stored = made(recipients=(("user", str(U)),)).to_dict()
    for body in (stored, for_wire(stored)):
        got = w.GenerationWorkflowInput.from_dict(body)
        assert (got.tenant_id, got.principal_id, got.artefact_id) == (str(T), str(U), str(A))
        assert got.generation_run_id == stored["generation_run_id"]
        assert (got.kind, got.origin, got.outline_gate, got.plan) == (
            "document",
            "original",
            True,
            "standard",
        )
        assert got.scope_seed == w.ScopeSeed(str(TENDER), (str(COLLECTION),), ("tender",))
        assert got.recipients == (w.Recipient("user", str(U)),)
        assert got.instruction_ref is None and not got.is_regeneration and got.extras == {}


def test_a_null_plan_absent_recipients_and_a_gate_the_api_turned_off_decode() -> None:
    got = w.GenerationWorkflowInput.from_dict(made(plan=None, outline_gate=False).to_dict())
    assert got.plan is None and got.recipients == () and got.outline_gate is False


def test_w6_extras_are_flattened_to_the_top_level_and_come_back_as_extras() -> None:
    body = made(
        kind="extraction",
        extras={"schema_ref": "schema-7", "trigger_document_version_id": str(BASE)},
    ).to_dict()
    assert body["schema_ref"] == "schema-7"  # the API flattens them
    got = w.GenerationWorkflowInput.from_dict(body)
    assert got.kind == "extraction"
    assert dict(got.extras) == {"schema_ref": "schema-7", "trigger_document_version_id": str(BASE)}


def test_a5_the_wire_form_carries_a_reference_and_the_stored_form_is_read_the_same_way() -> None:
    stored = made(
        origin="regeneration", base_version_id=BASE, outline_gate=False, instruction="shorter"
    ).to_dict()
    assert stored["instruction"] == "shorter"  # the database keeps the words
    wire = for_wire(stored)
    assert "instruction" not in wire and wire["instruction_ref"] == stored["generation_run_id"]
    for body in (stored, wire):  # a restart reads the stored one, a start gets the wire one
        got = w.GenerationWorkflowInput.from_dict(body)
        assert got.is_regeneration and got.base_version_id == str(BASE)
        assert got.instruction_ref == stored["generation_run_id"] and not got.outline_gate
        assert "shorter" not in repr(got)  # the decoded input never holds the words


def test_a_missing_required_key_is_refused_and_an_unknown_one_is_kept_not_fatal() -> None:
    body = made().to_dict()
    del body["tenant_id"]
    with pytest.raises(KeyError, match="tenant_id"):
        w.GenerationWorkflowInput.from_dict(body)
    future = made().to_dict() | {"a_future_identifier": "x"}
    assert dict(w.GenerationWorkflowInput.from_dict(future).extras) == {"a_future_identifier": "x"}


def test_workflow_signal_and_workflow_names_match_what_the_api_sends() -> None:
    assert w.SIGNAL_APPROVE == api_ports.SIGNAL_APPROVE
    assert w.SIGNAL_REVISE == api_ports.SIGNAL_REVISE
    assert w.SIGNAL_CANCEL == api_ports.SIGNAL_CANCEL
    assert w.SIGNAL_RESUME == api_ports.SIGNAL_RESUME
    assert w.WORKFLOW_GENERATION == api_ports.WORKFLOW_GENERATION
    assert w.WORKFLOW_EXTRACTION == api_ports.WORKFLOW_EXTRACTION


def test_llm_models_imports_only_the_standard_library() -> None:
    """A workflow imports it inside Temporal's sandbox: nothing from ``rag`` or a third party."""
    root = Path(w.__file__).parent
    stdlib = sys.stdlib_module_names
    for path in root.glob("*.py"):
        for node in ast.walk(ast.parse(path.read_text())):
            names = (
                [a.name for a in node.names]
                if isinstance(node, ast.Import)
                else [node.module or ""]
                if isinstance(node, ast.ImportFrom) and node.level == 0
                else []
            )
            for name in names:
                assert name.split(".")[0] in stdlib | {"llm_models"}, f"{path.name} imports {name}"
