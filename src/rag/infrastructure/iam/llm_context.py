"""IAM User Profile ``llm-context`` (prompt layer 3, §22.5, §14.3).

``GET /api/v1/internal/users/:id/llm-context``: 300 ms, no retry, a breaker, cached 300 s per user
(§14.3). The body is IAM's ``LLMContextResponse`` (``iam-lld-user-profile`` §5.3 #13); the fields
this service reads are the ones LLD §22.10 admits to layer 3. ``credentials``, ``locale`` and
``timezone`` are never read, so they are dropped before the cache.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
from fastapicommon.logging import get_logger
from fastapicommon.propagation import create_platform_http_client

from rag.application.generation.ports import UserContext, UserContextUnavailable
from rag.application.retrieval.ports import KeyValueCache
from rag.domain.shared.identity import RequestIdentity
from rag.infrastructure.resilience import BreakerOpen, CircuitBreaker

log = get_logger("rag.llm_context")
TTL_SECONDS = 300
TIMEOUT_SECONDS = 0.3
READ_FIELDS = (
    "display_name",
    "job_title",
    "writing_tone",
    "templates",
    "context_documents",
    "preferred_model",
)


def _strings(value: Any) -> list[str]:
    return [v for v in value or [] if isinstance(v, str) and v] if isinstance(value, list) else []


def _text(body: dict[str, Any]) -> UserContext | None:
    lines: list[str] = []
    for field, label in (
        ("display_name", "Name"),
        ("job_title", "Job title"),
        ("writing_tone", "Preferred tone"),
    ):
        if isinstance(body.get(field), str) and body[field]:
            lines.append(f"{label}: {body[field]}")
    lines += [f"Template: {t}" for t in _strings(body.get("templates"))]
    lines += [f"Context document: {d}" for d in _strings(body.get("context_documents"))]
    model = body.get("preferred_model")
    if not lines and not model:
        return None
    return UserContext("\n".join(lines), str(model) if model else None)


class HttpUserContext:
    """``UserContextProvider`` over IAM. Any failure is ``UserContextUnavailable``."""

    def __init__(self, base_url: str, cache: KeyValueCache | None = None) -> None:
        self._base_url, self._cache = base_url, cache
        self._breaker = CircuitBreaker(name="llm_context")

    async def get(self, identity: RequestIdentity) -> UserContext | None:
        """The caller's layer-3 context, or ``None`` where IAM holds none."""
        key = f"{identity.tenant_id}:llmctx:{identity.principal_id}"
        if self._cache is not None and (hit := await self._cache.get(key)) is not None:
            return _text(json.loads(hit))

        async def fetch() -> dict[str, Any]:
            async with create_platform_http_client(
                base_url=self._base_url, timeout=TIMEOUT_SECONDS
            ) as client:
                response = await client.get(
                    f"/api/v1/internal/users/{identity.principal_id}/llm-context",
                    headers={
                        "x-tenant-id": str(identity.tenant_id),
                        "x-user-id": str(identity.principal_id),
                    },
                )
                if response.status_code == 404:
                    return {}
                response.raise_for_status()
                body = response.json()
                if not isinstance(body, dict):
                    raise ValueError("llm-context is not an object")
                return {k: body.get(k) for k in READ_FIELDS}

        try:
            body = await self._breaker.call(fetch)
        except (BreakerOpen, httpx.HTTPError, ValueError) as exc:
            log.warning("llm_context_unavailable", error=type(exc).__name__)
            raise UserContextUnavailable from exc
        if self._cache is not None:
            await self._cache.set(key, json.dumps(body), TTL_SECONDS)
        return _text(body)
