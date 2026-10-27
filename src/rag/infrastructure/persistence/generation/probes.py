"""GEN-3 store probe and the COL-14 tender gate: small reads the admission path needs."""

from __future__ import annotations

import asyncio
from uuid import UUID

from rag.application.shared.ports import TransactionRunner, TxHandle
from rag.domain.shared.errors import DomainError, ErrorCode

PROBE_TIMEOUT_SECONDS = 0.1


class PgStoreProbe:
    """``StoreProbe``: ``llm_generation`` must accept a write transaction (GEN-3).

    100 ms, no retry, no breaker: the failure is wanted (§14.3). A read-only primary, an
    exhausted pool, an unreachable server all answer ``503 ARTEFACT_STORE_UNWRITABLE`` before a
    token is spent.
    """

    def __init__(self, tx: TransactionRunner) -> None:
        self._tx = tx

    async def require_writable(self) -> None:
        """Raise ``ARTEFACT_STORE_UNWRITABLE`` unless a read-write transaction opens."""

        async def probe(tx: TxHandle) -> str:
            return str(await tx.fetchval("SELECT current_setting('transaction_read_only')"))

        try:
            state = await asyncio.wait_for(self._tx.run_in_tx(probe), PROBE_TIMEOUT_SECONDS)
        except Exception as exc:
            raise DomainError(
                ErrorCode.ARTEFACT_STORE_UNWRITABLE, "The artefact store is not writable."
            ) from exc
        if state != "off":
            raise DomainError(
                ErrorCode.ARTEFACT_STORE_UNWRITABLE, "The artefact store is not writable."
            )


class CorpusTenderGate:
    """``TenderGate``: a tender with ``tender_closed_at`` takes no new work (COL-14, ART-7).

    Reads ``llm_corpus`` ``FOR SHARE`` in its own transaction (a transaction cannot span the two
    instances), so a close that commits afterwards is caught by the archive consumer, which
    makes the artefact read-only (ART-7) and lets a run in flight complete.
    """

    def __init__(self, corpus_tx: TransactionRunner) -> None:
        self._tx = corpus_tx

    async def require_open(self, tender_id: UUID) -> None:
        """Raise ``409 TENDER_CLOSED`` where any collection of the tender is closed."""

        async def closed(tx: TxHandle) -> bool:
            row = await tx.fetchval(
                "SELECT 1 FROM collections WHERE tender_id = $1 AND tender_closed_at IS NOT NULL "
                "LIMIT 1 FOR SHARE",
                tender_id,
            )
            return row is not None

        if await self._tx.run_in_tx(closed):
            raise DomainError(ErrorCode.TENDER_CLOSED, "The tender has closed.")
