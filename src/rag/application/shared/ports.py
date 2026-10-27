"""Outbound ports every module shares."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any, Protocol

type TxHandle = Any
"""Opaque transaction-bound connection handed to repository methods.

Only infrastructure interprets it (an ``asyncpg`` connection under ``pgcommon``); application
code passes it through and never calls it.
"""


class TransactionRunner(Protocol):
    """Runs a unit of work in one database transaction on one Postgres instance.

    The tenant GUC is bound by the pool's provider inside the same transaction (§4.8.1), so
    every statement ``fn`` issues is subject to RLS. Commits on return, rolls back on raise.
    """

    async def run_in_tx[T](
        self, fn: Callable[[TxHandle], Awaitable[T]], *, read_only: bool = False
    ) -> T:
        """Run ``fn`` in a transaction and return its result."""
        ...
