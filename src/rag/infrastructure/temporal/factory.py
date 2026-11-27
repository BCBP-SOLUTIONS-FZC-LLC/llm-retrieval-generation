"""Construction of the Temporal client, the queue names and the sweep's starter from settings."""

from __future__ import annotations

from pathlib import Path

from temporalio.service import TLSConfig

from rag.application.workflows.bridges import OrphanRestarter
from rag.application.workflows.ports import WorkflowClient
from rag.application.workflows.start import QueueNames
from rag.infrastructure.config import LOCAL_ENVIRONMENTS, ConfigError, Settings
from rag.infrastructure.temporal.client import TemporalWorkflowClient
from rag.infrastructure.temporal.unwired import UnwiredWorkflowClient


def queue_names(settings: Settings) -> QueueNames:
    """The six queues (WP-10): generation and extraction, each shared, enterprise and strict."""
    return QueueNames(
        settings.temporal_task_queue,
        settings.temporal_task_queue_enterprise,
        settings.temporal_task_queue_strict,
        settings.temporal_task_queue_extraction,
        settings.temporal_task_queue_extraction_enterprise,
        settings.temporal_task_queue_extraction_strict,
    )


def assert_temporal_config(settings: Settings) -> None:
    """Refuse a half-set connection (fail closed, like every other setting that weakens a control).

    * A certificate needs its key, and a key its certificate.
    * Outside local, dev and test the namespace must be named: the cluster is shared with the
      Workflow Execution service and runs one namespace per environment (workers LLD WF-2), so a
      default would send production traffic to the wrong one.
    """
    if bool(settings.temporal_tls_cert_path) != bool(settings.temporal_tls_key_path):
        raise ConfigError("TEMPORAL_TLS_CERT_PATH and TEMPORAL_TLS_KEY_PATH are set together")
    if (
        settings.environment not in LOCAL_ENVIRONMENTS
        and "temporal_namespace" not in settings.model_fields_set
    ):
        raise ConfigError(
            f"TEMPORAL_NAMESPACE is required when APP_ENV is {settings.environment!r} "
            "(one namespace per environment)"
        )


def _read(path: str) -> bytes:
    return Path(path).read_bytes()


def _tls(settings: Settings) -> TLSConfig | None:
    if not settings.temporal_tls_cert_path:
        return None
    return TLSConfig(
        client_cert=_read(settings.temporal_tls_cert_path),
        client_private_key=_read(settings.temporal_tls_key_path),
    )


def build_workflow_client(settings: Settings) -> WorkflowClient:
    """The Temporal client (lazy connection), or the fail-closed stand-in without an address."""
    if not settings.temporal_address:
        return UnwiredWorkflowClient()
    assert_temporal_config(settings)
    return TemporalWorkflowClient.for_address(
        settings.temporal_address, settings.temporal_namespace, _tls(settings)
    )


def build_orphan_restarter(
    settings: Settings, client: WorkflowClient | None = None
) -> OrphanRestarter:
    """The sweep runner's starter: it needs the engine and the queues and nothing else."""
    return OrphanRestarter(client or build_workflow_client(settings), queue_names(settings))
