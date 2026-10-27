"""C-M1: a proxy of the Model Gateway's catalogue (§5.7.18)."""

from __future__ import annotations

from rag.application.corpus.dtos import ModelCatalogueDto
from rag.application.corpus.ports import ModelCatalogue


class GetModelCatalogue:
    """Holds no model list of its own; the Gateway re-validates on every call."""

    def __init__(self, catalogue: ModelCatalogue) -> None:
        self._catalogue = catalogue

    async def execute(self) -> ModelCatalogueDto:
        """The catalogue, or ``INFERENCE_UNAVAILABLE`` where it cannot be read."""
        return await self._catalogue.list_models()
