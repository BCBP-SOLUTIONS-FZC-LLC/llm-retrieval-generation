"""The Temporal adapter over a fake ``temporalio`` client: start idempotency, retries, signals."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from temporalio.common import WorkflowIDConflictPolicy, WorkflowIDReusePolicy
from temporalio.exceptions import WorkflowAlreadyStartedError
from temporalio.service import RPCError, RPCStatusCode
from tests.support_workflows import FakeWorkflows

from rag.application.workflows.bridges import OrphanRestarter
from rag.application.workflows.ports import (
    SIGNAL_APPROVE,
    SIGNAL_CANCEL,
    StartOutcome,
    StartRequest,
)
from rag.application.workflows.start import QueueNames
from rag.domain.shared.errors import DomainError, ErrorCode
from rag.infrastructure.config import Settings
from rag.infrastructure.temporal.client import TemporalWorkflowClient
from rag.infrastructure.temporal.factory import build_workflow_client, queue_names
from rag.infrastructure.temporal.unwired import UnwiredWorkflowClient

REQUEST = StartRequest("wf-1", "GenerationWorkflow", "llm-generation", {"tenant_id": "t"})


def rpc(status: RPCStatusCode) -> RPCError:
    return RPCError("boom", status, b"")


class FakeHandle:
    def __init__(self, client: FakeTemporal, workflow_id: str) -> None:
        self.client, self.workflow_id = client, workflow_id

    async def signal(self, name: str, arg: Any = None, *, rpc_timeout: Any = None) -> None:
        self.client.signal_calls.append((self.workflow_id, name, arg))
        if self.client.signal_errors:
            raise self.client.signal_errors.pop(0)


class FakeTemporal:
    """The two calls of ``temporalio.client.Client`` the adapter uses."""

    def __init__(self) -> None:
        self.start_calls: list[dict[str, Any]] = []
        self.start_errors: list[Exception] = []
        self.signal_calls: list[tuple[str, str, Any]] = []
        self.signal_errors: list[Exception] = []
        self.hang = False

    async def start_workflow(self, workflow: str, arg: Any, **kw: Any) -> None:
        self.start_calls.append({"workflow": workflow, "arg": arg, **kw})
        if self.hang:
            await asyncio.sleep(3600)
        if self.start_errors:
            raise self.start_errors.pop(0)

    def get_workflow_handle(self, workflow_id: str) -> FakeHandle:
        return FakeHandle(self, workflow_id)


class Rig:
    def __init__(self, **kw: Any) -> None:
        self.temporal, self.sleeps, self.connects = FakeTemporal(), [], 0

        async def connect() -> FakeTemporal:
            self.connects += 1
            return self.temporal

        async def sleep(seconds: float) -> None:
            self.sleeps.append(seconds)

        self.client = TemporalWorkflowClient(connect, sleep=sleep, **kw)  # type: ignore[arg-type]


async def test_start_sends_the_deterministic_id_queue_input_and_reject_duplicate() -> None:
    r = Rig()
    assert await r.client.start(REQUEST) is StartOutcome.STARTED
    call = r.temporal.start_calls[0]
    assert (call["workflow"], call["id"], call["task_queue"], call["arg"]) == (
        "GenerationWorkflow",
        "wf-1",
        "llm-generation",
        {"tenant_id": "t"},
    )
    assert call["id_reuse_policy"] is WorkflowIDReusePolicy.REJECT_DUPLICATE  # §9.2
    assert call["id_conflict_policy"] is WorkflowIDConflictPolicy.FAIL


async def test_an_already_started_workflow_is_success_not_an_error() -> None:
    r = Rig()
    r.temporal.start_errors = [WorkflowAlreadyStartedError("wf-1", "GenerationWorkflow")]
    assert await r.client.start(REQUEST) is StartOutcome.ALREADY_STARTED  # ASYNC-2
    assert r.sleeps == []  # the engine answered: nothing to retry


async def test_a_restart_may_reuse_the_id_of_a_closed_workflow() -> None:
    r = Rig()
    await r.client.start(StartRequest("wf-1", "GenerationWorkflow", "q", {}, restart=True))
    assert r.temporal.start_calls[0]["id_reuse_policy"] is WorkflowIDReusePolicy.ALLOW_DUPLICATE
    assert r.temporal.start_calls[0]["id_conflict_policy"] is WorkflowIDConflictPolicy.FAIL


async def test_two_retries_with_backoff_then_success() -> None:
    r = Rig()
    r.temporal.start_errors = [rpc(RPCStatusCode.UNAVAILABLE), rpc(RPCStatusCode.UNAVAILABLE)]
    assert await r.client.start(REQUEST) is StartOutcome.STARTED
    assert len(r.temporal.start_calls) == 3 and r.sleeps == [0.2, 0.5]  # §14.3: 2 retries


async def test_an_engine_that_stays_down_is_503_after_three_attempts() -> None:
    r = Rig()
    r.temporal.start_errors = [rpc(RPCStatusCode.UNAVAILABLE)] * 5
    with pytest.raises(DomainError) as exc:
        await r.client.start(REQUEST)
    assert exc.value.code is ErrorCode.WORKFLOW_ENGINE_UNAVAILABLE
    assert len(r.temporal.start_calls) == 3


async def test_a_not_found_rpc_on_start_is_503_not_a_raw_error() -> None:
    r = Rig()
    r.temporal.start_errors = [rpc(RPCStatusCode.NOT_FOUND)]  # a wrong namespace, say
    with pytest.raises(DomainError) as exc:
        await r.client.start(REQUEST)
    assert exc.value.code is ErrorCode.WORKFLOW_ENGINE_UNAVAILABLE
    assert len(r.temporal.start_calls) == 1  # not retried


async def test_a_hung_call_times_out_and_is_503() -> None:
    r = Rig(timeout_seconds=0.01)
    r.temporal.hang = True
    with pytest.raises(DomainError) as exc:
        await r.client.start(REQUEST)
    assert exc.value.code is ErrorCode.WORKFLOW_ENGINE_UNAVAILABLE


async def test_the_connection_is_lazy_and_a_connect_failure_is_503_not_a_crash() -> None:
    async def connect() -> Any:
        raise OSError("refused")

    async def sleep(_: float) -> None:
        return None

    client = TemporalWorkflowClient(connect, sleep=sleep)  # building it connects to nothing
    with pytest.raises(DomainError) as exc:
        await client.start(REQUEST)
    assert exc.value.code is ErrorCode.WORKFLOW_ENGINE_UNAVAILABLE


async def test_signal_addresses_the_run_workflow_and_carries_identifiers_only() -> None:
    r = Rig()
    await r.client.signal("wf-1", SIGNAL_APPROVE, {"outline_version": 2})
    await r.client.signal("wf-1", SIGNAL_CANCEL)
    assert r.temporal.signal_calls == [
        ("wf-1", SIGNAL_APPROVE, {"outline_version": 2}),
        ("wf-1", SIGNAL_CANCEL, None),
    ]
    assert r.connects == 1  # one connection, reused


async def test_a_signal_to_a_workflow_that_no_longer_exists_is_503_and_not_retried() -> None:
    r = Rig()
    r.temporal.signal_errors = [rpc(RPCStatusCode.NOT_FOUND)]
    with pytest.raises(DomainError) as exc:
        await r.client.signal("wf-1", SIGNAL_CANCEL)
    assert exc.value.code is ErrorCode.WORKFLOW_ENGINE_UNAVAILABLE
    assert "rematerialise" in (exc.value.detail or "")  # the zombie run's way out (W-8)
    assert len(r.temporal.signal_calls) == 1 and r.sleeps == []


async def test_a_transient_signal_failure_is_retried() -> None:
    r = Rig()
    r.temporal.signal_errors = [rpc(RPCStatusCode.UNAVAILABLE)]
    await r.client.signal("wf-1", SIGNAL_CANCEL)
    assert len(r.temporal.signal_calls) == 2


# --- wiring: the fail-closed stand-in, the queues, the sweep's starter ---------------------------
def test_no_address_means_the_fail_closed_stand_in() -> None:
    client = build_workflow_client(Settings(temporal_address=""))
    assert isinstance(client, UnwiredWorkflowClient) and client.available is False


def test_an_address_builds_a_lazy_client_in_the_llm_generation_namespace() -> None:
    s = Settings(temporal_address="temporal.internal:7233")
    assert s.temporal_namespace == "llm-generation"  # §9.1
    client = build_workflow_client(s)
    assert isinstance(client, TemporalWorkflowClient) and client.available  # no I/O yet


def test_queue_names_come_from_settings() -> None:
    q = queue_names(Settings())
    assert (q.default, q.enterprise, q.strict) == (
        "llm-generation",
        "llm-generation-enterprise",
        "llm-generation-strict",
    )


async def test_the_unwired_client_refuses_both_calls() -> None:
    client = UnwiredWorkflowClient()
    for call in (client.start(REQUEST), client.signal("wf", SIGNAL_CANCEL)):
        with pytest.raises(DomainError) as exc:
            await call
        assert exc.value.code is ErrorCode.WORKFLOW_ENGINE_UNAVAILABLE


QUEUES = QueueNames("q", "q-ent", "q-strict")


async def test_the_orphan_sweeps_starter_restarts_from_the_stored_input() -> None:
    wf = FakeWorkflows()
    starter = OrphanRestarter(wf, QUEUES)
    await starter.start(
        "art-1", {"kind": "document", "plan": "enterprise", "residency_mode": "standard"}
    )
    await starter.start("art-2", {"kind": "extraction", "residency_mode": "strict"})
    first, second = wf.starts
    assert (first.workflow_type, first.task_queue, first.restart) == (
        "GenerationWorkflow",
        "q-ent",
        False,
    )
    assert (second.workflow_type, second.task_queue) == ("ExtractionWorkflow", "q-strict")


async def test_the_starter_refuses_a_run_with_no_workflow_so_the_sweep_fails_it() -> None:
    starter = OrphanRestarter(FakeWorkflows(), QUEUES)
    for wid, body in ((None, {"kind": "document"}), ("x", None), ("x", {"synchronous": True})):
        with pytest.raises(DomainError):
            await starter.start(wid, body)  # type: ignore[arg-type]


def test_the_starter_is_unavailable_exactly_when_the_client_is() -> None:
    assert OrphanRestarter(FakeWorkflows(available=False), QUEUES).available is False
    assert OrphanRestarter(FakeWorkflows(), QUEUES).available is True
