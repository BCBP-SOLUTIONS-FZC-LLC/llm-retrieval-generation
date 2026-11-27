"""The prefix of every Tier 3 metric this code registers (``<domain>_<service>_``).

fastapicommon refuses a name outside the active identity's prefix, so the prefix is read when a
collector is built, not frozen at import. The API (``llm-retrieval-generation``) gets
``llm_retrieval_generation_``, exactly as before; a Worker Pool process that embeds the engine
under its own identity (``llm-workers``) gets ``llm_workers_`` (engine 1.2.0, ADR-0003 gap 6).
"""

from __future__ import annotations

from fastapicommon.metrics import metrics_identity
from fastapicommon.metrics.registry import service_prefix

API_PREFIX = "llm_retrieval_generation_"


def metric_prefix() -> str:
    """The active identity's prefix; the API's own before metrics are initialised."""
    identity = metrics_identity()
    return API_PREFIX if identity is None else service_prefix(identity.domain, identity.service)
