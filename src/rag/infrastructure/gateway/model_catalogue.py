"""``ModelCatalogue`` over the Model Gateway's catalogue (C-M1 proxies it, §5.7.18, §22.1)."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import httpx
from fastapicommon.propagation import create_platform_http_client

from rag.application.corpus.dtos import ModelCatalogueDto, ModelDto
from rag.domain.shared.errors import DomainError, ErrorCode
from rag.infrastructure.config import Settings

"""Assumption: the Gateway's route-configuration listing. The LLD names no path (§22.1, OQ-92)."""
CATALOGUE_TIMEOUT_SECONDS = 2.0


def parse_catalogue(body: Any, now: datetime) -> ModelCatalogueDto:
    """The Gateway's ``{models: [{id, residency, task_classes, plans}], as_of?}`` as a DTO."""
    models = [
        ModelDto(
            id=str(m["id"]),
            residency=str(m.get("residency", "")),
            task_classes=[str(t) for t in m.get("task_classes", [])],
            plans=[str(p) for p in m.get("plans", [])],
        )
        for m in body["models"]
    ]
    as_of = body.get("as_of")
    return ModelCatalogueDto(
        models, datetime.fromisoformat(as_of.replace("Z", "+00:00")) if as_of else now
    )


class HttpModelCatalogue:
    """Reads the catalogue per call.

    C-M1 has one caller every five minutes, so a client per call is cheaper than owning a
    client lifecycle (ponytail: pool it if that changes).
    """

    def __init__(self, settings: Settings) -> None:
        self._base_url = settings.gateway_base_url
        self._path = settings.gateway_models_path

    async def list_models(self) -> ModelCatalogueDto:
        """Raises ``INFERENCE_UNAVAILABLE`` where the Gateway's catalogue cannot be read."""
        try:
            async with create_platform_http_client(
                base_url=self._base_url, timeout=CATALOGUE_TIMEOUT_SECONDS
            ) as client:
                response = await client.get(self._path)
                response.raise_for_status()
                return parse_catalogue(response.json(), datetime.now(UTC))
        except (httpx.HTTPError, ValueError, KeyError, TypeError, AttributeError) as exc:
            raise DomainError(
                ErrorCode.INFERENCE_UNAVAILABLE, "The model catalogue cannot be read."
            ) from exc
