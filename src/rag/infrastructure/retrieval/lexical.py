"""Lexical search over Ingestion's chunk rows in ``llm_corpus``: read-only (DEP-5, §15.1).

Ingestion owns that table; this repo does not migrate it. The table and column names below
are an ASSUMPTION (``LEXICAL_TABLE`` is configurable): ``tenant_id, tender_id, collection_id,
index_generation, is_current, chunk_id, document_id, document_version, document_title,
document_role, language, heading_path, clause_ref, parent_chunk_id, text, tsv`` with ``tsv``
a ``tsvector`` built with the ``simple`` configuration. Every predicate is the SCOPE-2 compound
for each collection of the token, with ``tenant_id`` leading, plus ``is_current`` (RET-8).
Reads go to the primary: replica routing (§4.9) is not built.
"""

from __future__ import annotations

import asyncio
import re
from typing import Any

from rag.application.retrieval.ports import SearchHalfError
from rag.application.shared.ports import TransactionRunner, TxHandle
from rag.corpusscope import ScopeToken
from rag.domain.retrieval.candidate import Candidate
from rag.domain.retrieval.plan import StructuralFilters
from rag.domain.retrieval.scope import ResolvedScope
from rag.infrastructure.resilience import CircuitBreaker

LEXICAL_TIMEOUT = 0.2
TEXT_CONFIG = "simple"
_IDENT = re.compile(r"^[a-z_][a-z0-9_]*$")


def build_sql(table: str, *, with_roles: bool, with_language: bool) -> str:
    """The lexical statement. ``table`` is validated as a bare identifier by the caller.

    Parameters: ``$1`` query, ``$2..$5`` the compound per collection, then the optional role
    list and language, then ``LIMIT`` last.
    """
    extra, n = "", 6
    if with_roles:
        extra += f" AND c.document_role = ANY(${n}::text[])"
        n += 1
    if with_language:
        extra += f" AND lower(split_part(c.language, '-', 1)) = ${n}"
        n += 1
    return (
        "SELECT c.chunk_id, c.document_id, c.document_version, c.collection_id, "
        "c.document_title, c.document_role, c.language, c.heading_path, c.clause_ref, "
        "c.parent_chunk_id, c.text, ts_rank_cd(c.tsv, q.query) AS score "
        f"FROM {table} c "
        "JOIN unnest($2::uuid[], $3::uuid[], $4::uuid[], $5::int[]) "
        "  AS s(tenant_id, tender_id, collection_id, generation) "
        "  ON c.tenant_id = s.tenant_id AND c.tender_id = s.tender_id "
        "  AND c.collection_id = s.collection_id AND c.index_generation = s.generation, "
        f"websearch_to_tsquery('{TEXT_CONFIG}', $1) AS q(query) "
        f"WHERE c.is_current AND c.tsv @@ q.query{extra} "
        f"ORDER BY score DESC, c.chunk_id LIMIT ${n}"
    )


class PgLexicalSearch:
    """``LexicalSearch`` through the corpus runner (tenant GUC bound, RLS applies)."""

    def __init__(self, tx: TransactionRunner, table: str, breaker: CircuitBreaker) -> None:
        if not _IDENT.fullmatch(table):
            raise ValueError("lexical table must be a bare lower-case identifier")
        self._tx, self._table, self._breaker = tx, table, breaker

    async def search(
        self,
        scope: ResolvedScope,
        query: str,
        *,
        limit: int,
        filters: StructuralFilters,
    ) -> list[Candidate]:
        """Best-first by ``ts_rank_cd``. ``SearchHalfError`` on timeout, outage or open breaker."""
        if not query.strip():
            return []
        handle = scope.handle
        if not isinstance(handle, ScopeToken):
            raise SearchHalfError("the scope carries no token")
        tokens = {c.id: c for c in handle.collections}
        rows_in = [tokens[c.id] for c in scope.searchable if c.id in tokens]
        if not rows_in:
            return []
        weights = scope.weights()
        sql = build_sql(
            self._table,
            with_roles=bool(filters.document_roles),
            with_language=bool(filters.language),
        )
        args: list[Any] = [
            query,
            [t.tenant_id for t in rows_in],
            [t.tender_id for t in rows_in],
            [t.id for t in rows_in],
            [t.index_generation for t in rows_in],
        ]
        if filters.document_roles:
            args.append(list(filters.document_roles))
        if filters.language:
            args.append(filters.language.split("-")[0].lower())
        args.append(limit)

        async def work(tx: TxHandle) -> list[Candidate]:
            rows = await tx.fetch(sql, *args)
            return [
                Candidate(
                    chunk_id=r["chunk_id"],
                    document_id=r["document_id"],
                    document_version=int(r["document_version"]),
                    collection_id=r["collection_id"],
                    text=r["text"],
                    document_title=r["document_title"],
                    document_role=r["document_role"],
                    language=r["language"] or "und",
                    weight=weights[r["collection_id"]],
                    heading_path=r["heading_path"],
                    clause_ref=r["clause_ref"],
                    parent_chunk_id=r["parent_chunk_id"],
                    source_half="lexical",
                )
                for r in rows
            ]

        try:
            return await self._breaker.call(
                lambda: asyncio.wait_for(self._tx.run_in_tx(work, read_only=True), LEXICAL_TIMEOUT)
            )
        except Exception as exc:
            raise SearchHalfError("lexical search failed") from exc
