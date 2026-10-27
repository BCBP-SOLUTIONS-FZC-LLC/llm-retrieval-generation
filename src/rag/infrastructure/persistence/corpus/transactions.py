"""``TransactionRunner`` for ``llm_corpus`` that reports an unreachable store as §24 503."""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from pgcommon import (
    AcquireTimeoutError,
    Pool,
    is_connection_exception,
    is_connection_lost,
    is_pool_closed,
)

from rag.application.shared.ports import TxHandle
from rag.domain.shared.errors import DomainError, ErrorCode
from rag.infrastructure.persistence.transactions import PgTransactionRunner


class CorpusTransactionRunner(PgTransactionRunner):
    """``PgTransactionRunner`` mapping connection failures to ``503 SCOPE_STORE_UNAVAILABLE``.

    The collection tables are what scope resolution and C-I1 depend on; where they cannot be
    reached the request is refused and never given a default (CG-4).
    """

    def __init__(self, pool: Pool) -> None:
        super().__init__(pool)

    async def run_in_tx[T](
        self, fn: Callable[[TxHandle], Awaitable[T]], *, read_only: bool = False
    ) -> T:
        """Run ``fn`` in a transaction on ``llm_corpus``."""
        try:
            return await super().run_in_tx(fn, read_only=read_only)
        except DomainError:
            raise
        except Exception as exc:
            if (
                is_connection_exception(exc)
                or is_connection_lost(exc)
                or is_pool_closed(exc)
                or isinstance(exc, AcquireTimeoutError | OSError)
            ):
                raise DomainError(
                    ErrorCode.SCOPE_STORE_UNAVAILABLE, "The collection store is unreachable."
                ) from exc
            raise
