"""Fail-closed stand-in for the Temporal client where no engine is configured (S-1, WP-12).

With ``TEMPORAL_ADDRESS`` unset the async entry points answer ``503
WORKFLOW_ENGINE_UNAVAILABLE`` before admission, the orphan sweep skips instead of failing rows it
could not retry, and the quota consumer raises (the message retries and, failing that,
dead-letters) instead of reporting a resume.
"""

from __future__ import annotations

from typing import Any

from rag.application.workflows.ports import StartOutcome, StartRequest
from rag.domain.shared.errors import DomainError, ErrorCode


class UnwiredWorkflowClient:
    """``WorkflowClient`` that refuses. ``available`` is ``False``."""

    available = False

    async def start(self, request: StartRequest) -> StartOutcome:
        """Always refused."""
        raise DomainError(ErrorCode.WORKFLOW_ENGINE_UNAVAILABLE, "No workflow engine is wired.")

    async def signal(
        self, workflow_id: str, signal: str, payload: dict[str, Any] | None = None
    ) -> None:
        """Always refused."""
        raise DomainError(ErrorCode.WORKFLOW_ENGINE_UNAVAILABLE, "No workflow engine is wired.")
