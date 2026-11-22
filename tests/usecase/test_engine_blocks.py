"""The engine's building-block facades over stand-ins: error translation, pinned pack, correlation
id, the stored instruction, and W-6's extraction body against the same fakes R-4 is tested with."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from dataclasses import dataclass, replace
from typing import Any
from uuid import UUID, uuid4

import pytest
from tests.support_generate import SCHEMA, TENANT, TENDER, GenRig
from tests.support_retrieval import FakePacks, FakeScopeProvider, identity, scope
from tests.usecase.test_extract import CMD, rig

from llm_models.workers import GenerationWorkflowInput
from rag.application.generation.flow import AdmissionRequest, Admitted
from rag.application.retrieval.scope import ScopeSpec
from rag.application.shared.gateway import (
    Completion,
    GatewayDeadlineExceeded,
    GatewayQuotaRefused,
    GatewayResidencyRefused,
    GatewayUnavailable,
    StreamChunk,
)
from rag.application.verification.gateway import GatewayEntailer, GatewayJudge, GatewayResampler
from rag.application.verification.verify import VerificationRequest
from rag.domain.generation.prompt import compose_prompt
from rag.domain.shared.enums import ArtefactKind, ArtefactStatus, GrantLevel, RunStage, RunStatus
from rag.domain.shared.errors import DomainError, ErrorCode
from rag.domain.workflows.input import ScopeSeed, WorkflowInput
from rag.engine import blocks as b
from rag.engine.context import EngineServices, RunContext
from rag.engine.errors import (
    InferenceUnavailable,
    QuotaRefused,
    ResidencyRefused,
    RunGone,
    SchemaViolation,
    ScopeRevoked,
)

PRINCIPAL = identity().principal_id


def winput(**kw: Any) -> GenerationWorkflowInput:
    body = WorkflowInput(
        tenant_id=TENANT,
        principal_id=PRINCIPAL,
        artefact_id=uuid4(),
        generation_run_id=uuid4(),
        capability="technical_proposal",
        requested_mode="assistance",
        plan="enterprise",
        residency_mode="strict",
        domain_profile_version="dp-9",
        language_pack_version="lp-9",
        scope_seed=ScopeSeed(TENDER),
        tender_id=TENDER,
        correlation_id="corr-1",
        **kw,
    ).to_dict()
    return GenerationWorkflowInput.from_dict(body)


class Tx:
    async def run_in_tx(self, fn: Any, *, read_only: bool = False) -> Any:
        return await fn(None)


class StoredInstruction:
    def __init__(self, words: str | None) -> None:
        self.words = words

    async def stored_instruction(self, tx: Any, run_id: UUID) -> str | None:
        return self.words


def ctx_with(
    blocks: b.Blocks | Any, inp: GenerationWorkflowInput | None = None, **svc: Any
) -> RunContext:
    services = EngineServices(
        tx=svc.get("tx", Tx()),  # type: ignore[arg-type]
        runs=svc.get("runs"),  # type: ignore[arg-type]
        writes=None,  # type: ignore[arg-type]
        facts=None,  # type: ignore[arg-type]
        engine=svc.get("engine"),  # type: ignore[arg-type]
        events=None,  # type: ignore[arg-type]
        audit=None,  # type: ignore[arg-type]
        clock=None,  # type: ignore[arg-type]
        store=None,  # type: ignore[arg-type]
        blocks=blocks,
    )
    return RunContext(services, inp or winput())


@dataclass
class Stub:
    """Whatever collaborators a facade reaches for; the attributes a test sets are the ones used."""

    def __getattr__(self, name: str) -> Any:
        raise AssertionError(f"unexpected use of {name}")


def blocks_with(**kw: Any) -> Any:
    s = Stub()
    for k, v in kw.items():
        object.__setattr__(s, k, v)
    return s


@pytest.mark.parametrize(
    ("raised", "expected"),
    [
        (GatewayQuotaRefused(), QuotaRefused),
        (GatewayResidencyRefused(), ResidencyRefused),
        (GatewayUnavailable(), InferenceUnavailable),
        (GatewayDeadlineExceeded(), InferenceUnavailable),
        (TimeoutError(), InferenceUnavailable),
        (DomainError(ErrorCode.QUOTA_EXHAUSTED), QuotaRefused),
        (DomainError(ErrorCode.RESIDENCY_CAPACITY), ResidencyRefused),
        (DomainError(ErrorCode.INFERENCE_UNAVAILABLE), InferenceUnavailable),
        (DomainError(ErrorCode.COLLECTION_NOT_FOUND), ScopeRevoked),
        (DomainError(ErrorCode.GRANT_LEVEL_INSUFFICIENT), ScopeRevoked),
        (DomainError(ErrorCode.EXTRACTION_SCHEMA_VIOLATION), SchemaViolation),
    ],
)
def test_every_refusal_becomes_one_typed_engine_error_with_a_content_free_message(
    raised: Exception, expected: type[Exception]
) -> None:
    got = b.translate(raised)
    assert isinstance(got, expected) and str(got) == str(expected())


def test_an_unrelated_error_comes_back_unchanged() -> None:
    other = DomainError(ErrorCode.ARTEFACT_NOT_FOUND)
    assert b.translate(other) is other
    assert b.translate(ValueError("x")).args == ("x",)


def test_the_gateway_context_carries_run_section_attempt_as_the_correlation_id() -> None:
    inp = winput()
    g = b.gateway_context(ctx_with(None, inp), "2.1", 3)
    assert g.correlation_id == f"{inp.generation_run_id}:2.1:3"
    assert (g.plan, g.residency_mode, g.tenant_id) == ("enterprise", "strict", TENANT)


async def test_the_pack_is_read_at_the_version_the_run_pinned() -> None:
    packs = FakePacks()
    seen: dict[str, Any] = {}
    real = packs.resolve

    async def spy(tenant_id: UUID, **kw: Any) -> Any:
        seen.update(kw)
        return await real(tenant_id, **kw)

    packs.resolve = spy  # type: ignore[method-assign]
    await b.load_pack(ctx_with(blocks_with(packs=packs)))
    assert seen["pinned_version"] == "dp-9"  # never the latest (ASYNC-3)


class Directory:
    async def identity_for(self, tenant_id: UUID, principal_id: UUID, **kw: Any) -> Any:
        assert (tenant_id, principal_id) == (TENANT, PRINCIPAL) and kw["residency_mode"] == "strict"
        return identity()


async def test_reresolve_reads_iam_and_reports_counts_and_an_empty_scope_is_revoked() -> None:
    provider = FakeScopeProvider(scope())
    c = ctx_with(blocks_with(scope=provider, directory=Directory()))
    snap = await b.reresolve(c)
    assert (snap.collections, snap.narrowed) == (1, False)
    revoked = FakeScopeProvider(error=DomainError(ErrorCode.COLLECTION_NOT_FOUND))
    with pytest.raises(ScopeRevoked):
        await b.reresolve(ctx_with(blocks_with(scope=revoked, directory=Directory())))
    down = FakeScopeProvider(error=DomainError(ErrorCode.SCOPE_STORE_UNAVAILABLE))
    with pytest.raises(DomainError) as exc:  # fails closed: never a default scope (SCOPE-6)
        await b.reresolve(ctx_with(blocks_with(scope=down, directory=Directory())))
    assert exc.value.code is ErrorCode.SCOPE_STORE_UNAVAILABLE


class Gateway:
    def __init__(self, *, error: Exception | None = None) -> None:
        self.error, self.calls = error, []

    def generate_stream(
        self, task: str, system: str, user: str, *, ctx: Any, deadline_seconds: float
    ) -> AsyncIterator[StreamChunk]:
        self.calls.append(("stream", task, ctx.correlation_id))

        async def gen() -> AsyncIterator[StreamChunk]:
            if self.error:
                raise self.error
            yield StreamChunk("Hello ", model="m-1", serving_tier="primary")
            yield StreamChunk(
                "world", prompt_tokens=11, completion_tokens=7, finish_reason="length"
            )

        return gen()

    async def complete(
        self, task: str, system: str, user: str, *, ctx: Any, deadline_seconds: float
    ) -> Completion:
        self.calls.append(("complete", task, ctx.correlation_id))
        if self.error:
            raise self.error
        return Completion('{"ok": 1}', model="m-2")


def prompt() -> Any:
    return compose_prompt(
        task="draft",
        skeleton="",
        tenant_overrides="",
        user_context="",
        chunks=[],
        user_prompt="write",
    )


async def test_a_draft_streams_and_is_collected_and_a_json_task_is_one_completion() -> None:
    gw = Gateway()
    c = ctx_with(blocks_with(gateway=gw))
    got = await b.call_model(c, "draft", prompt(), section_ref="3", attempt=2, deadline_seconds=5)
    assert (got.text, got.model, got.serving_tier) == ("Hello world", "m-1", "primary")
    assert (got.prompt_tokens, got.completion_tokens, got.finish_reason) == (11, 7, "length")
    done = await b.call_model(
        c, "extract", prompt(), section_ref="3", attempt=1, deadline_seconds=5
    )
    assert (done.text, done.model) == ('{"ok": 1}', "m-2")
    run = c.input.generation_run_id
    assert gw.calls == [("stream", "draft", f"{run}:3:2"), ("complete", "extract", f"{run}:3:1")]


@pytest.mark.parametrize("task", ["draft", "extract", "refine-query"])
async def test_a_quota_refusal_is_typed_from_every_model_calling_block(task: str) -> None:
    c = ctx_with(blocks_with(gateway=Gateway(error=GatewayQuotaRefused())))
    with pytest.raises(QuotaRefused):  # G-2: the stream, the JSON tasks, not only write_section
        await b.call_model(c, task, prompt(), section_ref="1", attempt=1, deadline_seconds=5)


async def test_verification_ports_use_the_runs_gateway_context() -> None:
    seen: dict[str, Any] = {}

    class Verifier:
        async def execute(self, req: VerificationRequest) -> str:
            seen["req"] = req
            return "result"

    c = ctx_with(blocks_with(gateway=Gateway(), verifier=Verifier()))
    req = VerificationRequest(text="t", evidence=[], refs={}, section_label="body")
    out = await b.verify(c, req, prompt=prompt(), deadline_seconds=9, section_ref="4", attempt=2)
    wired = seen["req"]
    assert out == "result" and wired.section_label == "body"
    assert isinstance(wired.entailer, GatewayEntailer) and isinstance(wired.judge, GatewayJudge)
    assert isinstance(wired.resampler, GatewayResampler)


async def test_the_refiner_is_given_the_words_read_back_from_the_run_not_from_the_input() -> None:
    seen: dict[str, Any] = {}

    class Refiner:
        async def refine(self, **kw: Any) -> str:
            seen.update(kw)
            return "refined"

    c = ctx_with(
        blocks_with(refiner=Refiner()),
        winput(origin="regeneration", base_version_id=uuid4(), instruction="shorter"),
        engine=StoredInstruction("make it shorter"),
    )
    assert "shorter" not in repr(c.input)  # the decoded input holds a reference only
    out = await b.refine_instruction(
        c, section_text="s", section_type="body", mandatory_clauses=["m"], facts=[], bundle=None
    )  # type: ignore[arg-type]
    assert out == "refined" and seen["instruction"] == "make it shorter"
    assert seen["ctx"].correlation_id.endswith(":body:1")
    none = ctx_with(blocks_with(), engine=StoredInstruction(None))
    assert (
        await b.refine_instruction(
            none, section_text="s", section_type="body", mandatory_clauses=[], facts=[], bundle=None
        )
        is None
    )  # type: ignore[arg-type]


# --- W-6: an extraction for a run that already exists --------------------------------------------
def extraction_request() -> AdmissionRequest:
    return AdmissionRequest(
        kind=ArtefactKind.EXTRACTION,
        capability="extract",
        section_type=SCHEMA.ref,
        user_text="",
        scope=ScopeSpec(TENDER),
        required_level=GrantLevel.GENERATE,
        tender_id=TENDER,
        stage=RunStage.EXTRACT,
    )


async def admitted_run(r: GenRig) -> Admitted:
    adm = await r.admit.execute_async(identity(), extraction_request())
    run = adm.run
    run.move_to(RunStatus.GENERATING)  # begin_stage(extract) opened the run and the artefact
    adm.artefact.move_to(ArtefactStatus.GENERATING)
    adm.section.start()  # and the extraction's one section opens with it
    prep = await r.admit.prepare(identity(), extraction_request())
    return Admitted(
        identity=identity(), request=extraction_request(), scope=prep.scope, bundle=prep.bundle,
        pack_markers=tuple(prep.markers), eligibility=prep.eligibility, profile=prep.profile,
        artefact=adm.artefact, run=run, section=adm.section,
    )  # fmt: skip


async def test_run_admitted_extracts_for_an_existing_run_and_does_not_start_it_again() -> None:
    r = rig()
    a = await admitted_run(r)
    out = await r.extract.run_admitted(a, replace(CMD, trigger_document_version_id="dv-9"), SCHEMA)
    assert out.fields["contract_value"]["value"] == 4820000 and out.artefact_version_id is not None
    assert r.repo.run.status is RunStatus.COMPLETE
    assert "ExtractionCompleted" in r.events.types()  # the same write transaction as R-4's


async def test_run_admitted_closes_the_run_on_a_schema_violation_as_r4_does() -> None:
    r = rig()
    r.stream_gw.extract_text = json.dumps(
        {"fields": {"contract_value": {"value": "ten", "chunk_ids": ["c-01"]}}}
    )
    a = await admitted_run(r)
    with pytest.raises(DomainError) as exc:
        await r.extract.run_admitted(a, CMD, SCHEMA)
    assert exc.value.code is ErrorCode.EXTRACTION_SCHEMA_VIOLATION
    assert r.repo.run.status is RunStatus.FAILED  # so the workflow's fail_run finds it terminal
    assert not r.repo.completions


class Rows:
    """The stored rows ``run_extraction`` rebuilds the admission from."""

    def __init__(self, a: Admitted | None) -> None:
        self.a = a

    async def get_run(self, tx: Any, run_id: UUID, **kw: Any) -> Any:
        return None if self.a is None else self.a.run

    async def get_artefact(self, tx: Any, artefact_id: UUID, **kw: Any) -> Any:
        return None if self.a is None else self.a.artefact

    async def list_sections(self, tx: Any, artefact_id: UUID) -> list[Any]:
        return [] if self.a is None else [self.a.section]

    async def save_section_state(self, tx: Any, section: Any) -> None:
        self.saved = section.state


def extraction_input(
    a: Admitted | None, schema_ref: str | None = SCHEMA.ref
) -> GenerationWorkflowInput:
    run = a.run if a else None
    return GenerationWorkflowInput.from_dict(
        WorkflowInput(
            tenant_id=TENANT,
            principal_id=PRINCIPAL,
            artefact_id=a.artefact.id if a else uuid4(),
            generation_run_id=run.id if run else uuid4(),
            capability="extract",
            requested_mode="assistance",
            plan=None,
            residency_mode="standard",
            domain_profile_version="dp-1",
            language_pack_version="lp-1",
            scope_seed=ScopeSeed(TENDER),
            tender_id=TENDER,
            kind="extraction",
            outline_gate=False,
            extras={} if schema_ref is None else {"schema_ref": schema_ref},
        ).to_dict()
    )


async def test_run_extraction_rebuilds_the_admission_from_the_rows_and_returns_a_plain_result() -> (
    None
):
    r = rig()
    a = await admitted_run(r)
    rows = Rows(a)
    c = ctx_with(
        blocks_with(directory=Directory2(), admit=r.admit, extractor=r.extract),
        extraction_input(a),
        runs=rows,
        engine=rows,
    )
    got = await b.run_extraction(c)
    assert got.state == "complete" and got.evidence_tier == "SUPPORTED" and got.artefact_version_id
    # an insufficient retrieval is a result, not an error: the run is closed, the workflow ends
    r2 = rig()
    r2.dense.hits = []
    a2 = await admitted_run(r2)
    rows2 = Rows(a2)
    c2 = ctx_with(
        blocks_with(directory=Directory2(), admit=r2.admit, extractor=r2.extract),
        extraction_input(a2),
        runs=rows2,
        engine=rows2,
    )
    refused = await b.run_extraction(c2)
    assert (refused.state, refused.refusal_reason) == ("refused", "insufficient_evidence")


async def test_run_extraction_with_no_row_is_run_gone_and_without_a_schema_ref_is_refused() -> None:
    rows = Rows(None)
    c = ctx_with(
        blocks_with(directory=Directory2()), extraction_input(None), runs=rows, engine=rows
    )
    with pytest.raises(RunGone):
        await b.run_extraction(c)
    with pytest.raises(ValueError, match="schema_ref"):
        await b.run_extraction(ctx_with(blocks_with(), extraction_input(None, None)))


class Directory2:
    async def identity_for(self, tenant_id: UUID, principal_id: UUID, **kw: Any) -> Any:
        return identity()
