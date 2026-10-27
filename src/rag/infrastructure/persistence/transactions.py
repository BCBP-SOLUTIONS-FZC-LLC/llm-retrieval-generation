"""``pgcommon``-backed implementation of the ``TransactionRunner`` port."""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from pgcommon import Pool, run_in_tx

from rag.application.shared.ports import TxHandle


class PgTransactionRunner:
    """Runs units of work through ``pgcommon.run_in_tx`` on one pool."""

    def __init__(self, pool: Pool) -> None:
        self._pool = pool

    async def run_in_tx[T](
        self, fn: Callable[[TxHandle], Awaitable[T]], *, read_only: bool = False
    ) -> T:
        """Run ``fn`` in a transaction (commit on return, rollback on raise)."""
        return await run_in_tx(self._pool, fn, read_only=read_only)
