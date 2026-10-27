"""IAM User Profile ``llm-context`` (prompt layer 3, §22.5, §14.3).

``GET /users/:id/llm-context``: 300 ms, no retry, a breaker, cached 300 s per user (§14.3).
The body shape is assumed (the LLD names the path and the fields, not the document):
``{"tone": str|null, "credentials": [str], "snippets": [str], "preferred_model": str|null}``.
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


def _text(body: dict[str, Any]) -> UserContext | None:
    lines: list[str] = []
    if body.get("tone"):
        lines.append(f"Preferred tone: {body['tone']}")
    lines += [f"Credential: {c}" for c in body.get("credentials") or []]
    lines += [f"Snippet: {s}" for s in body.get("snippets") or []]
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
                    f"/users/{identity.principal_id}/llm-context",
                    headers={"x-tenant-id": str(identity.tenant_id)},
                )
                if response.status_code == 404:
                    return {}
                response.raise_for_status()
                body = response.json()
                if not isinstance(body, dict):
                    raise ValueError("llm-context is not an object")
                return body

        try:
            body = await self._breaker.call(fetch)
        except (BreakerOpen, httpx.HTTPError, ValueError) as exc:
            log.warning("llm_context_unavailable", error=type(exc).__name__)
            raise UserContextUnavailable from exc
        if self._cache is not None:
            await self._cache.set(key, json.dumps(body), TTL_SECONDS)
        return _text(body)
