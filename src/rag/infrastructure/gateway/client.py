"""The Model Gateway HTTP adapter (§22.1, DEP-2: the only inference-backend client).

The Gateway is Envoy AI Gateway, whose routes this team writes (LD-68). The paths below are
the OpenAI-compatible ones plus ``/v1/rerank`` and vLLM's ``/tokenize``: an assumption the
route configuration is the specification for (§22.1, OQ-92/93). A task class is the virtual
model name. Refusals map from the status: ``429`` quota, a ``residency_capacity`` error code
residency; anything else that fails is unavailable.
"""

from __future__ import annotations

import asyncio
import json
import math
import time
from collections.abc import AsyncIterator
from typing import Any

import httpx
from fastapicommon.logging import get_logger
from fastapicommon.propagation import create_platform_http_client

from rag.application.shared.gateway import (
    TASK_EMBED,
    TASK_RERANK,
    Completion,
    GatewayContext,
    GatewayDeadlineExceeded,
    GatewayError,
    GatewayQuotaRefused,
    GatewayResidencyRefused,
    GatewayUnavailable,
    StreamChunk,
)
from rag.infrastructure.config import Settings
from rag.infrastructure.resilience import BreakerOpen, CircuitBreaker

log = get_logger("rag.gateway")
EMBED_TIMEOUT = 0.3
TOKENIZE_TIMEOUT = 0.3
TOKENIZER_MODEL = "draft"  # the generation Model Profile's tokenizer (§6.8)


def _headers(ctx: GatewayContext) -> dict[str, str]:
    headers = {"x-residency-mode": ctx.residency_mode}
    if ctx.plan:
        headers["x-plan"] = ctx.plan
    if ctx.preferred_model:
        headers["x-preferred-model"] = ctx.preferred_model  # a hint; the Gateway re-validates
    return headers


def _refusal(response: httpx.Response) -> GatewayError:
    if response.status_code == 429:
        retry = response.headers.get("retry-after", "")
        return GatewayQuotaRefused(int(retry) if retry.isdigit() else None)
    try:
        code = response.json()["error"]["code"]
    except (ValueError, KeyError, TypeError):
        code = None
    if code == "residency_capacity":
        return GatewayResidencyRefused("no in-region capacity")
    return GatewayUnavailable(f"gateway answered {response.status_code}")


def _chunk(payload: str, tier: str) -> StreamChunk | None:
    """One SSE ``data:`` payload as a chunk; ``None`` for a frame with neither text nor usage."""
    try:
        obj = json.loads(payload)
        if "error" in obj:
            raise GatewayUnavailable("the stream carried an error")
        choices = obj.get("choices") or []
        delta = (choices[0].get("delta") or {}).get("content") or "" if choices else ""
        usage = obj.get("usage") or {}
        if not delta and not usage:
            return None
        return StreamChunk(
            str(delta),
            str(obj.get("model", "")),
            tier,
            int(usage.get("prompt_tokens", 0)),
            int(usage.get("completion_tokens", 0)),
        )
    except (ValueError, TypeError, AttributeError, IndexError) as exc:
        raise GatewayUnavailable("the stream carried a malformed frame") from exc


class HttpModelGateway:
    """``ModelGateway`` over HTTP. One lazy client; call ``aclose`` at shutdown."""

    def __init__(self, settings: Settings) -> None:
        self._base_url = settings.gateway_base_url
        self._client: httpx.AsyncClient | None = None
        self._embed_breaker = CircuitBreaker(name="gateway_embed")

    def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = create_platform_http_client(base_url=self._base_url)
        return self._client

    async def aclose(self) -> None:
        """Close the HTTP client."""
        if self._client is not None:
            await self._client.aclose()

    async def _post(
        self, path: str, body: dict[str, Any], ctx: GatewayContext, timeout: float
    ) -> dict[str, Any]:
        try:
            response = await self._http().post(
                path, json=body, headers=_headers(ctx), timeout=timeout
            )
        except httpx.TimeoutException as exc:
            log.warning("gateway_timeout", path=path, timeout=timeout)
            raise GatewayDeadlineExceeded(f"{path} timed out") from exc
        except httpx.HTTPError as exc:
            log.warning("gateway_unreachable", path=path, error=type(exc).__name__)
            raise GatewayUnavailable(f"{path} unreachable") from exc
        if response.status_code >= 400:
            log.warning("gateway_refused", path=path, status=response.status_code)
            raise _refusal(response)
        try:
            data = response.json()
        except ValueError as exc:
            raise GatewayUnavailable(f"{path} returned a malformed body") from exc
        if not isinstance(data, dict):
            raise GatewayUnavailable(f"{path} returned a malformed body")
        return data

    async def embed(self, texts: list[str], *, ctx: GatewayContext) -> list[list[float]]:
        """300 ms, one immediate retry, breaker (opens on 5 failures, half-open after 30 s)."""

        async def once() -> list[list[float]]:
            data = await self._post(
                "/v1/embeddings", {"model": TASK_EMBED, "input": texts}, ctx, EMBED_TIMEOUT
            )
            try:
                return [[float(x) for x in item["embedding"]] for item in data["data"]]
            except (KeyError, TypeError, ValueError) as exc:
                raise GatewayUnavailable("embeddings body is malformed") from exc

        async def with_retry() -> list[list[float]]:
            try:
                return await once()
            except (GatewayQuotaRefused, GatewayResidencyRefused):
                raise  # a refusal is an answer; retrying it only spends budget
            except GatewayError:
                return await once()

        try:
            return await self._embed_breaker.call(with_retry)
        except BreakerOpen as exc:
            raise GatewayUnavailable("embed breaker is open") from exc

    async def rerank(
        self, query: str, passages: list[str], *, ctx: GatewayContext, deadline_seconds: float
    ) -> list[float]:
        """No retry (§6.7): a retried rerank has already spent the budget it protects."""
        data = await self._post(
            "/v1/rerank",
            {"model": TASK_RERANK, "query": query, "documents": passages},
            ctx,
            deadline_seconds,
        )
        try:
            scores = [0.0] * len(passages)
            for item in data["results"]:
                scores[int(item["index"])] = float(item["relevance_score"])
            return scores
        except (KeyError, TypeError, ValueError, IndexError) as exc:
            raise GatewayUnavailable("rerank body is malformed") from exc

    async def complete(
        self,
        task_class: str,
        system: str,
        user: str,
        *,
        ctx: GatewayContext,
        deadline_seconds: float,
    ) -> Completion:
        """Temperature 0, JSON object; no retry here (the Gateway owns provider fallback)."""
        data = await self._post(
            "/v1/chat/completions",
            {
                "model": task_class,
                "temperature": 0,
                "response_format": {"type": "json_object"},
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
            },
            ctx,
            deadline_seconds,
        )
        try:
            return Completion(
                str(data["choices"][0]["message"]["content"]), str(data.get("model", ""))
            )
        except (KeyError, IndexError, TypeError) as exc:
            raise GatewayUnavailable("completion body is malformed") from exc

    async def generate_stream(
        self,
        task_class: str,
        system: str,
        user: str,
        *,
        ctx: GatewayContext,
        deadline_seconds: float,
        temperature: float = 0.2,
    ) -> AsyncIterator[StreamChunk]:
        """OpenAI-compatible SSE (``stream: true``, usage on the last chunk) on ``task_class``.

        The status is read before the first delta, so a quota, residency or outage refusal
        raises on the first ``__anext__`` (API-6). ``deadline_seconds`` bounds the whole stream.
        No retry: the Gateway owns provider fallback (§14.3).
        """
        body = {
            "model": task_class,
            "temperature": temperature,
            "stream": True,
            "stream_options": {"include_usage": True},
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        }
        started = time.monotonic()
        try:
            async with self._http().stream(
                "POST",
                "/v1/chat/completions",
                json=body,
                headers=_headers(ctx),
                timeout=httpx.Timeout(deadline_seconds, connect=5.0),
            ) as response:
                if response.status_code >= 400:
                    await response.aread()
                    log.warning("gateway_refused", path="stream", status=response.status_code)
                    raise _refusal(response)
                tier = response.headers.get("x-serving-tier", "")
                async for line in response.aiter_lines():
                    if time.monotonic() - started > deadline_seconds:
                        raise GatewayDeadlineExceeded("stream exceeded its deadline")
                    if not line.startswith("data:"):
                        continue  # comments, event names, blank separators
                    payload = line[5:].strip()
                    if payload == "[DONE]":
                        return
                    chunk = _chunk(payload, tier)
                    if chunk is not None:
                        yield chunk
        except httpx.TimeoutException as exc:
            log.warning("gateway_timeout", path="stream", timeout=deadline_seconds)
            raise GatewayDeadlineExceeded("stream timed out") from exc
        except httpx.HTTPError as exc:
            log.warning("gateway_unreachable", path="stream", error=type(exc).__name__)
            raise GatewayUnavailable("stream unreachable") from exc

    async def count(self, texts: list[str], *, ctx: GatewayContext) -> list[int]:
        """``TokenCounter``: the generation profile's tokenizer through ``/tokenize`` (§6.8).

        Where the Gateway cannot tokenize, a conservative estimate (3 characters per token)
        over-counts rather than under-reserving output room, and the call never fails.
        """

        async def one(text: str) -> int:
            try:
                data = await self._post(
                    "/tokenize", {"model": TOKENIZER_MODEL, "prompt": text}, ctx, TOKENIZE_TIMEOUT
                )
                return int(data["count"])
            except (GatewayError, KeyError, TypeError, ValueError):
                return math.ceil(len(text) / 3)

        return list(await asyncio.gather(*(one(t) for t in texts)))
