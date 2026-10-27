"""R-1: hybrid retrieval with no generation (§5.7.1)."""

from __future__ import annotations

from rag.application.retrieval.dtos import SearchCommand, SearchResult
from rag.application.retrieval.pipeline import RetrievalPipeline
from rag.application.retrieval.record import RecordRetrievalSet
from rag.domain.shared.identity import RequestIdentity


class SearchDocuments:
    """Runs the pipeline through stage 9 and writes the retrieval set (``artefact_id`` null).

    A ``failed`` verdict raises ``503 RETRIEVAL_FAILED`` from the pipeline and writes nothing.
    A write failure is ``503 ARTEFACT_STORE_UNWRITABLE`` (the response carries an id that must
    resolve on R-5).
    """

    def __init__(self, pipeline: RetrievalPipeline, recorder: RecordRetrievalSet) -> None:
        self._pipeline, self._recorder = pipeline, recorder

    async def execute(self, identity: RequestIdentity, cmd: SearchCommand) -> SearchResult:
        """The recorded set's id and the outcome."""
        outcome = await self._pipeline.run(identity, cmd.request)
        recorded = await self._recorder.record(
            identity, outcome, tender_id=cmd.request.scope.tender_id
        )
        return SearchResult(recorded.retrieval_set_id, outcome, recorded.widened_retry_of)
