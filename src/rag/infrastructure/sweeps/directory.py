"""``TenantDirectory`` over the ``rag_sweeper`` enumeration role (migration 0003).

Every table is under ``FORCE ROW LEVEL SECURITY`` and no role is ``BYPASSRLS`` (§4.1), so "which
tenants have work" cannot be asked as ``rag_app``. The enumeration login is a member of
``rag_sweeper``, which holds ``SELECT`` on the filter columns only and a ``FOR SELECT`` policy
naming that role alone; the answer is tenant ids and nothing else. Every sweep then re-reads its
rows under the tenant's own scope.

ponytail: ``SELECT DISTINCT tenant_id`` over each candidate predicate; an index on the predicate
(the ones named in §4.x exist) keeps it cheap, a ``tenants`` cache is the fallback at scale.
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pgcommon import Pool

from rag.application.sweeps import sweeps as s

_GENERATION = {
    s.ORPHAN_ARTEFACTS: "SELECT DISTINCT tenant_id FROM artefacts "
    "WHERE status = 'pending' AND created_at < $1",
    s.STALLED_GENERATIONS: "SELECT DISTINCT tenant_id FROM artefacts "
    "WHERE status = 'generating' AND updated_at < $1",
    s.IDEMPOTENCY_KEYS: "SELECT DISTINCT tenant_id FROM generation_runs "
    "WHERE idempotency_key IS NOT NULL AND created_at < $1",
    s.RETRIEVAL_SETS: "SELECT DISTINCT tenant_id FROM retrieval_sets "
    "WHERE artefact_id IS NULL AND created_at < $1",
    s.GRAPH_CHECKPOINTS: "SELECT DISTINCT tenant_id FROM graph_checkpoints WHERE created_at < $1",
    s.AUDIT_CHAIN: "SELECT DISTINCT tenant_id FROM artefact_audit_chain WHERE created_at <= $1",
}
_CORPUS = {
    s.TENANT_PURGE: "SELECT tenant_id FROM tenant_lifecycle "
    "WHERE state = 'purged' AND changed_at < $1",
    s.STALLED_PROMOTIONS: "SELECT DISTINCT tenant_id FROM collections "
    "WHERE status = 'provisioning' AND promoted_from IS NOT NULL AND created_at < $1",
}


class PgTenantDirectory:
    """Lists tenants with candidate rows, through the two enumeration pools."""

    def __init__(self, corpus: Pool, generation: Pool) -> None:
        self._corpus, self._generation = corpus, generation

    async def tenants_with_work(self, sweep: str, before: datetime) -> list[UUID]:
        """Tenant ids with a candidate for ``sweep`` older than ``before``."""
        if sweep in _CORPUS:
            pool, sql = self._corpus, _CORPUS[sweep]
        else:
            pool, sql = self._generation, _GENERATION[sweep]

        async def rows(conn: object) -> list[UUID]:
            return [r["tenant_id"] for r in await conn.fetch(sql, before)]  # type: ignore[attr-defined]

        return await pool.with_conn(rows)
