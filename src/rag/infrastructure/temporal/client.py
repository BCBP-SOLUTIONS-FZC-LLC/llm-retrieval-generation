"""The Temporal client of §9.1 (LD-88): ``start_workflow`` and ``signal_workflow``, nothing else.

Namespace ``llm-generation``; 2 s per call, 2 retries with backoff (§14.3), no breaker. The
connection is lazy, so an engine that is down at startup does not stop the service: WP-12 says
R-1 to R-4 are unaffected and only the async entry points answer ``503``.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from datetime import timedelta
from typing import Any

from fastapicommon.logging import get_logger
from temporalio.client import Client
from temporalio.common import WorkflowIDConflictPolicy, WorkflowIDReusePolicy
from temporalio.exceptions import WorkflowAlreadyStartedError
from temporalio.service import RPCError, RPCStatusCode

from rag.application.workflows.ports import StartOutcome, StartRequest
from rag.domain.shared.errors import DomainError, ErrorCode

log = get_logger(__name__)
CALL_TIMEOUT_SECONDS = 2.0
RETRIES = 2
BACKOFF_SECONDS = (0.2, 0.5)

type Sleep = Callable[[float], Awaitable[None]]


def _unavailable(detail: str) -> DomainError:
    return DomainError(ErrorCode.WORKFLOW_ENGINE_UNAVAILABLE, detail)


class TemporalWorkflowClient:
    """``WorkflowClient`` over ``temporalio``.

    The sweep's and the quota consumer's ports wrap it in ``rag.application.workflows.bridges``.

    ``connect`` is injected so tests drive a fake client; the real one connects on first use.
    """

    available = True

    def __init__(
        self,
        connect: Callable[[], Awaitable[Client]],
        *,
        sleep: Sleep = asyncio.sleep,
        timeout_seconds: float = CALL_TIMEOUT_SECONDS,
    ) -> None:
        self._connect, self._sleep, self._timeout = connect, sleep, timeout_seconds
        self._client: Client | None = None
        self._lock = asyncio.Lock()

    @classmethod
    def for_address(cls, address: str, namespace: str) -> TemporalWorkflowClient:
        """A client that connects to ``address`` / ``namespace`` lazily."""

        async def connect() -> Client:
            return await Client.connect(address, namespace=namespace, lazy=True)

        return cls(connect)

    async def _get(self) -> Client:
        async with self._lock:
            if self._client is None:
                self._client = await self._connect()
            return self._client

    async def _with_retries[T](self, what: str, call: Callable[[Client], Awaitable[T]]) -> T:
        last: Exception | None = None
        for attempt in range(RETRIES + 1):
            try:
                client = await asyncio.wait_for(self._get(), self._timeout)
                return await asyncio.wait_for(call(client), self._timeout)
            except WorkflowAlreadyStartedError, DomainError:
                raise
            except RPCError as exc:
                if exc.status is RPCStatusCode.NOT_FOUND:
                    raise
                last = exc
            except (TimeoutError, OSError, RuntimeError) as exc:
                last = exc
            if attempt < RETRIES:
                await self._sleep(BACKOFF_SECONDS[min(attempt, len(BACKOFF_SECONDS) - 1)])
        log.warning("temporal_call_failed", operation=what, error=type(last).__name__)
        raise _unavailable("The workflow engine is unreachable.") from last

    async def start(self, request: StartRequest) -> StartOutcome:
        """Start once. ``already_started`` is the engine absorbing a repeat (ASYNC-2)."""
        reuse = (
            WorkflowIDReusePolicy.ALLOW_DUPLICATE
            if request.restart
            else WorkflowIDReusePolicy.REJECT_DUPLICATE
        )

        async def call(client: Client) -> None:
            await client.start_workflow(
                request.workflow_type,
                request.run_input,
                id=request.workflow_id,
                task_queue=request.task_queue,
                id_reuse_policy=reuse,
                id_conflict_policy=WorkflowIDConflictPolicy.FAIL,
                rpc_timeout=timedelta(seconds=self._timeout),
            )

        try:
            await self._with_retries("start", call)
        except WorkflowAlreadyStartedError:
            return StartOutcome.ALREADY_STARTED
        except RPCError as exc:  # NOT_FOUND (a wrong namespace, say) is no answer from the engine
            raise _unavailable("The workflow engine refused the start.") from exc
        return StartOutcome.STARTED

    async def signal(
        self, workflow_id: str, signal: str, payload: dict[str, Any] | None = None
    ) -> None:
        """Signal ``workflow_id``; a missing workflow is as good as an unreachable engine."""

        async def call(client: Client) -> None:
            handle = client.get_workflow_handle(workflow_id)
            if payload is None:
                await handle.signal(signal, rpc_timeout=timedelta(seconds=self._timeout))
            else:
                await handle.signal(signal, payload, rpc_timeout=timedelta(seconds=self._timeout))

        try:
            await self._with_retries("signal", call)
        except RPCError as exc:  # NOT_FOUND: the workflow is gone (zombie run, W-8 restarts it)
            raise _unavailable(
                "No workflow answers to the run's id; an operator can rematerialise it."
            ) from exc
