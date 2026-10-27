"""``CollectionStore`` over ``llm_corpus``: the resolver's one read (§6.2 steps 2, 6; §4.9)."""

from __future__ import annotations

from typing import Any
from uuid import UUID

from rag.application.shared.ports import TransactionRunner, TxHandle
from rag.corpusscope import CollectionRow

_SQL = """
SELECT c.id, c.tenant_id, c.type, c.tender_id, c.restricted, c.status, c.physical_collection,
       c.shard_key, c.purge_epoch, c.weight_override, c.owning_departments,
       c.restricted_announced_at,
       CASE WHEN c.type = 'reference' THEN c.active_index_generation
            ELSE (SELECT g.generation FROM index_generations g
                  WHERE g.collection_id = c.id AND g.status = 'active') END AS active_generation
FROM collections c
WHERE c.tenant_id = $1 OR c.type = 'reference'
"""
"""Reference rows carry the PLATFORM tenant, so ``index_generations``' tenant policy hides their
generations from a tenant session; for them the collection's own column is the only readable
answer (it is set with the first activation, §5.7.17). Every other row needs an ``active`` row."""


def _row(r: Any) -> CollectionRow:
    weight = r["weight_override"]
    return CollectionRow(
        id=r["id"],
        tenant_id=r["tenant_id"],
        type=str(r["type"]),
        tender_id=r["tender_id"],
        restricted=r["restricted"],
        status=str(r["status"]),
        physical_collection=r["physical_collection"],
        shard_key=r["shard_key"],
        purge_epoch=int(r["purge_epoch"]),
        active_generation=r["active_generation"],
        weight_override=None if weight is None else float(weight),
        owning_departments=tuple(r["owning_departments"]),
        restricted_announced_at=r["restricted_announced_at"],
    )


class PgCollectionStore:
    """Reads on the primary through the corpus runner, tenant GUC bound by the pool (SCOPE-10)."""

    def __init__(self, tx: TransactionRunner) -> None:
        self._tx = tx

    async def load(self, tenant_id: UUID) -> list[CollectionRow]:
        """The tenant's rows plus the platform reference rows, any status."""

        async def work(tx: TxHandle) -> list[CollectionRow]:
            return [_row(r) for r in await tx.fetch(_SQL, tenant_id)]

        return await self._tx.run_in_tx(work, read_only=True)
