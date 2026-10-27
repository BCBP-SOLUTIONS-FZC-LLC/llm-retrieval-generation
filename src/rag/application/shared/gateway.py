"""The Model Gateway port (§22.1): the one way this service reaches an inference backend.

The adapter lives in ``infrastructure.gateway`` (DEP-2). Retrieval uses ``embed``, ``rerank``
and ``complete`` (the refiner and the classification proposal). The generation slice adds
``generate_stream`` as a new method with its own result type; nothing here changes for it.
The Gateway's task classes are the virtual model names (§22.1).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Protocol

TASK_EMBED = "embed"
TASK_RERANK = "rerank"
TASK_REFINE_QUERY = "refine-query"
TASK_REFINE_INSTRUCTION = "refine-instruction"
TASK_EXTRACT = "extract"
TASK_DRAFT = "draft"
TASK_JUDGE = "judge"


@dataclass(frozen=True, slots=True)
class GatewayContext:
    """Per-request routing inputs the Gateway re-validates (§22.1): plan, residency, hint."""

    plan: str | None = None
    residency_mode: str = "standard"
    preferred_model: str | None = None


class GatewayError(Exception):
    """Base of every Gateway failure. ``reason`` is the §5.5 marker reason."""

    reason = "unavailable"


class GatewayUnavailable(GatewayError):  # noqa: N818
    """Down, 5xx, malformed answer, or the breaker is open."""

    reason = "unavailable"


class GatewayDeadlineExceeded(GatewayError):  # noqa: N818
    """The call exceeded its deadline and was cancelled."""

    reason = "deadline_exceeded"


class GatewayQuotaRefused(GatewayError):  # noqa: N818
    """Quota refusal (``429``). ``retry_after_seconds`` feeds ``Retry-After`` on R-6."""

    reason = "quota_refused"

    def __init__(self, retry_after_seconds: int | None = None) -> None:
        super().__init__("quota refused")
        self.retry_after_seconds = retry_after_seconds


class GatewayResidencyRefused(GatewayError):  # noqa: N818
    """A ``strict`` tenant has no in-region capacity (LD-74)."""

    reason = "residency_refused"


@dataclass(frozen=True, slots=True)
class Completion:
    """A ``complete`` result: the text and the model profile that produced it."""

    text: str
    model: str = ""


@dataclass(frozen=True, slots=True)
class StreamChunk:
    """One piece of a streamed generation (§22.1): a text delta, and what the Gateway reports.

    ``model`` and ``serving_tier`` are the Model Profile and provider tier that served the call;
    the token counts arrive on the last chunk when the Gateway reports usage.
    """

    delta: str = ""
    model: str = ""
    serving_tier: str = ""
    prompt_tokens: int = 0
    completion_tokens: int = 0


class ModelGateway(Protocol):
    """Embed, rerank, complete and stream. Every method raises a ``GatewayError`` only."""

    def generate_stream(
        self,
        task_class: str,
        system: str,
        user: str,
        *,
        ctx: GatewayContext,
        deadline_seconds: float,
        temperature: float = 0.2,
    ) -> AsyncIterator[StreamChunk]:
        """Stream prose on ``task_class`` (``draft``), with no retry (§14.3).

        Quota, residency and outage refusals raise on the first ``__anext__``, before any delta,
        so the caller can still answer with an HTTP status (API-6).
        """
        ...

    async def embed(self, texts: list[str], *, ctx: GatewayContext) -> list[list[float]]:
        """One vector per text (300 ms, one immediate retry, breaker; §14.3)."""
        ...

    async def rerank(
        self, query: str, passages: list[str], *, ctx: GatewayContext, deadline_seconds: float
    ) -> list[float]:
        """One relevance score per passage, in input order. Never retried (§6.7, RET-5)."""
        ...

    async def complete(
        self,
        task_class: str,
        system: str,
        user: str,
        *,
        ctx: GatewayContext,
        deadline_seconds: float,
    ) -> Completion:
        """A temperature-0 JSON completion on ``task_class`` (``refine-query``, ``extract``)."""
        ...


class TokenCounter(Protocol):
    """Counts tokens with the generation Model Profile's tokenizer (§6.8)."""

    async def count(self, texts: list[str], *, ctx: GatewayContext) -> list[int]:
        """Tokens per text. Where the Gateway cannot tokenize, a conservative estimate."""
        ...
