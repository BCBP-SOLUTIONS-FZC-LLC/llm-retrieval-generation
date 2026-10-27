"""Construction of the Temporal client, the queue names and the sweep's starter from settings."""

from __future__ import annotations

from rag.application.workflows.bridges import OrphanRestarter
from rag.application.workflows.ports import WorkflowClient
from rag.application.workflows.start import QueueNames
from rag.infrastructure.config import Settings
from rag.infrastructure.temporal.client import TemporalWorkflowClient
from rag.infrastructure.temporal.unwired import UnwiredWorkflowClient


def queue_names(settings: Settings) -> QueueNames:
    """The three generation queues (WP-10)."""
    return QueueNames(
        settings.temporal_task_queue,
        settings.temporal_task_queue_enterprise,
        settings.temporal_task_queue_strict,
    )


def build_workflow_client(settings: Settings) -> WorkflowClient:
    """The Temporal client (lazy connection), or the fail-closed stand-in without an address."""
    if not settings.temporal_address:
        return UnwiredWorkflowClient()
    return TemporalWorkflowClient.for_address(
        settings.temporal_address, settings.temporal_namespace
    )


def build_orphan_restarter(
    settings: Settings, client: WorkflowClient | None = None
) -> OrphanRestarter:
    """The sweep runner's starter: it needs the engine and the queues and nothing else."""
    return OrphanRestarter(client or build_workflow_client(settings), queue_names(settings))
