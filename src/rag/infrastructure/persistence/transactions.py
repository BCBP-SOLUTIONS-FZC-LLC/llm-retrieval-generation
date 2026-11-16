"""``pgcommon``-backed implementation of the ``TransactionRunner`` port."""

from __future__ import annotations

from collections.abc import Awaitable, Callable

import pgcommon
from fastapicommon.logging import get_logger
from pgcommon import AcquireTimeoutError, Pool, PoolClosedError, run_in_tx

from rag.application.shared.ports import TxHandle
from rag.domain.shared.errors import DomainError, ErrorCode

log = get_logger("rag.persistence")


def _database_unavailable(exc: Exception) -> bool:
    """The database could not be reached or ended the work: not a bug in a query.

    A refused connection is a plain ``OSError`` (asyncpg raises it for several addresses); the
    rest are the library's own classifiers. A slow query (``statement_timeout``) is not an outage.
    """
    return (
        isinstance(exc, OSError | AcquireTimeoutError | PoolClosedError)
        or pgcommon.is_connection_exception(exc)
        or pgcommon.is_connection_lost(exc)
        or pgcommon.is_operator_intervention(exc)
        or pgcommon.is_insufficient_resources(exc)
    )


class PgTransactionRunner:
    """Runs units of work through ``pgcommon.run_in_tx`` on one pool."""

    def __init__(self, pool: Pool, *, unavailable: ErrorCode | None = None) -> None:
        """``unavailable``: the §24 code a READ-ONLY unit of work answers when the database is out.

        Only ``read_only=True`` work is mapped: those are the request-path reads, where a raw
        connection error was a 500. Writes (admission, completion, consumers, relays) keep the
        original error, because their callers classify it and the consumers retry on it.
        """
        self._pool = pool
        self._unavailable = unavailable

    async def run_in_tx[T](
        self, fn: Callable[[TxHandle], Awaitable[T]], *, read_only: bool = False
    ) -> T:
        """Run ``fn`` in a transaction (commit on return, rollback on raise)."""
        try:
            return await run_in_tx(self._pool, fn, read_only=read_only)
        except DomainError:
            raise
        except Exception as exc:
            if read_only and self._unavailable is not None and _database_unavailable(exc):
                # the type name only, never the message (OBS-2)
                log.error("database_unavailable", error_type=type(exc).__name__)
                raise DomainError(self._unavailable, "The database is unavailable.") from exc
            raise
