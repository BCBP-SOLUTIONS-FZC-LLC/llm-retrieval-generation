"""The generation engine's failure and clean-up paths: what happens when something breaks.

Each test breaks one thing (an unexpected error, a drop, a failing close, a lost race) and checks
that the run ends in the right state and the caller sees the right problem (RUN-5, GEN-2, §7.5).
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from tests.support_generate import TENDER, GenRig
from tests.support_generation import TENANT
from tests.support_retrieval import chunk, identity
from tests.usecase.test_extract import CMD as EXTRACT_CMD
from tests.usecase.test_extract import rig as extract_rig
from tests.usecase.test_generate_short_form import Q, drain

from rag.application.generation.flow import AdmissionRequest
from rag.application.generation.frames import ErrorFrame, VerdictFrame
from rag.application.retrieval.scope import ScopeSpec
from rag.domain.generation.repositories import IdempotencyRace
from rag.domain.shared.enums import (
    ArtefactKind,
    ArtefactStatus,
    DeliveryState,
    GrantLevel,
    RunFailureReason,
    RunStatus,
)
from rag.domain.shared.errors import DomainError, ErrorCode
from rag.domain.shared.identity import RequestIdentity


async def boom(*_: Any, **__: Any) -> Any:
    raise RuntimeError("boom")


# --- before dispatch -----------------------------------------------------------------------


async def test_an_unexpected_error_before_dispatch_closes_the_run_and_is_raised() -> None:
    """Retrieval breaks with something nobody planned for: the run is cancelled `internal`."""
    rig = GenRig()
    rig.engine._retrieve = boom  # type: ignore[method-assign]
    with pytest.raises(RuntimeError):
        await rig.answer.execute(identity(), Q)
    assert rig.repo.run.status is RunStatus.CANCELLED
    assert rig.repo.artefact.status is ArtefactStatus.CANCELLED
    assert rig.stream_gw.stream_calls == []


async def test_a_caller_with_no_principal_cannot_start_generation() -> None:
    """API-1: generation needs a principal from the identity headers."""
    rig = GenRig()
    with pytest.raises(ValueError, match="principal"):
        await rig.answer.execute(RequestIdentity(TENANT, None), Q)
    assert not rig.repo.runs


async def test_a_contended_idempotency_key_is_a_409_after_the_attempts() -> None:
    """Every attempt loses the race to another admission: the caller gets a 409, not a hang."""
    rig = GenRig()

    async def lost(*_: Any, **__: Any) -> Any:
        raise IdempotencyRace

    rig.admit._commit = lost  # type: ignore[method-assign]
    with pytest.raises(DomainError) as exc:
        await rig.answer.execute(identity(), Q)
    assert exc.value.code is ErrorCode.IDEMPOTENCY_KEY_CONFLICT
    rig.admit._commit_async = lost  # type: ignore[method-assign]
    with pytest.raises(DomainError) as again:
        await rig.admit.execute_async(
            identity(),
            AdmissionRequest(
                kind=ArtefactKind.DOCUMENT,
                capability="technical_proposal",
                section_type="technical_proposal",
                user_text="",
                scope=ScopeSpec(TENDER),
                required_level=GrantLevel.GENERATE,
                tender_id=TENDER,
            ),
        )
    assert again.value.code is ErrorCode.IDEMPOTENCY_KEY_CONFLICT


# --- the producer --------------------------------------------------------------------------


async def test_a_timeout_cut_before_the_first_frame_marks_the_version_disconnected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#86: the platform's REQUEST_TIMEOUT cancels the wait; the version is not 'delivered'."""
    from rag.application.generation import generate as g

    async def cut(self: Any) -> None:
        raise asyncio.CancelledError

    monkeypatch.setattr(g._Producer, "first_byte", cut)
    rig = GenRig()
    with pytest.raises(asyncio.CancelledError):
        await rig.answer.execute(identity(), Q)
    await rig.settle()
    (rows,) = rig.repo.completions
    assert rows.artefact_version.delivery is DeliveryState.DISCONNECTED


async def test_a_producer_cancelled_at_shutdown_closes_the_run_failed() -> None:
    """The process stops mid-stream: the run is closed `failed`, not left `generating`."""
    rig = GenRig()
    rig.stream_gw.hold = asyncio.Event()  # the stream waits after its first delta
    stream = await rig.answer.execute(identity(), Q)
    for task in list(rig.engine._tasks):
        task.cancel()
    await asyncio.gather(*rig.engine._tasks, return_exceptions=True)
    assert rig.repo.run.status is RunStatus.FAILED
    assert rig.repo.run.failure_reason is RunFailureReason.INTERNAL
    _ = stream


async def test_a_run_that_cannot_start_is_cancelled_and_its_close_failure_is_only_logged() -> None:
    """The start fails, then closing the run fails too: the caller still gets the first error."""
    rig = GenRig()
    rig.engine._lifecycle.start = boom  # type: ignore[method-assign]
    with pytest.raises(RuntimeError):
        await rig.answer.execute(identity(), Q)
    assert rig.repo.run.status is RunStatus.CANCELLED  # not started, so cancelled, not failed
    rig2 = GenRig()
    rig2.engine._lifecycle.start = boom  # type: ignore[method-assign]
    rig2.engine._lifecycle.cancel = boom  # type: ignore[method-assign]
    with pytest.raises(RuntimeError):  # the close failure is swallowed (the sweep reaps it)
        await rig2.answer.execute(identity(), Q)


async def test_a_failed_store_after_the_stream_is_an_error_frame() -> None:
    """S3 fails after the stream began: the run fails and the caller gets an `error` frame."""
    rig = GenRig()
    rig.store.fail_put = True
    out = await drain(await rig.answer.execute(identity(), Q))
    assert out.of(ErrorFrame) and not out.of(VerdictFrame)
    assert rig.repo.run.status is RunStatus.FAILED
    assert rig.repo.run.failure_reason is RunFailureReason.ARTEFACT_STORE_UNWRITABLE


# --- extraction (R-4) ----------------------------------------------------------------------


async def test_an_extraction_that_breaks_before_dispatch_cancels_its_run() -> None:
    """The three exits of the pre-dispatch step: unexpected, refused and dropped."""
    r = extract_rig()
    r.extract._pre_dispatch = boom  # type: ignore[method-assign]
    with pytest.raises(RuntimeError):
        await r.extract.execute(identity(), EXTRACT_CMD)
    assert r.repo.run.status is RunStatus.CANCELLED

    r2 = extract_rig()

    async def refused(*_: Any, **__: Any) -> Any:
        raise DomainError(ErrorCode.REQUIRED_INPUT_MISSING, "x")

    r2.extract._pre_dispatch = refused  # type: ignore[method-assign]
    with pytest.raises(DomainError):
        await r2.extract.execute(identity(), EXTRACT_CMD)
    assert r2.repo.run.status is RunStatus.CANCELLED

    r3 = extract_rig()

    async def dropped(*_: Any, **__: Any) -> Any:
        raise asyncio.CancelledError

    r3.extract._pre_dispatch = dropped  # type: ignore[method-assign]
    with pytest.raises(asyncio.CancelledError):
        await r3.extract.execute(identity(), EXTRACT_CMD)
    assert r3.repo.run.status is RunStatus.CANCELLED


async def test_an_extraction_whose_close_fails_still_reports_the_original_problem() -> None:
    """The model answer breaks the schema and the run cannot be closed: the 422 still arrives."""
    r = extract_rig()
    r.stream_gw.extract_text = "not json at all"
    r.extract._lifecycle.fail = boom  # type: ignore[method-assign]
    with pytest.raises(DomainError) as exc:
        await r.extract.execute(identity(), EXTRACT_CMD)
    assert exc.value.code is ErrorCode.EXTRACTION_SCHEMA_VIOLATION


async def test_drain_waits_for_a_running_extraction() -> None:
    """Graceful shutdown (GEN-2): `drain` returns once the running extraction has finished."""
    r = extract_rig()
    r.stream_gw.extract_hold = asyncio.Event()
    task = asyncio.create_task(r.extract.execute(identity(), EXTRACT_CMD))
    while not r.stream_gw.completions:
        await asyncio.sleep(0)
    r.stream_gw.extract_hold.set()
    await r.extract.drain(5)
    await task
    assert len(r.repo.completions) == 1
    _ = chunk


async def test_a_problem_the_completion_already_names_is_not_rewrapped() -> None:
    """R-2: a DomainError from the write stays itself, so the run is `internal`, not `store`."""
    rig = GenRig()

    async def named(*_: Any, **__: Any) -> None:
        raise DomainError(ErrorCode.OPTIMISTIC_LOCK_CONFLICT, "named")

    rig.store.put = named  # type: ignore[method-assign]
    out = await drain(await rig.answer.execute(identity(), Q))
    assert out.of(ErrorFrame)
    assert rig.repo.run.status is RunStatus.FAILED
    assert rig.repo.run.failure_reason is RunFailureReason.INTERNAL
