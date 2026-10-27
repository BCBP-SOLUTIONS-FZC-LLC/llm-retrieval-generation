"""A chunk candidate moving through fusion, rerank and assembly."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from uuid import UUID


@dataclass(frozen=True, slots=True)
class Candidate:
    """One chunk with the provenance §4.4.2 records.

    ``text`` is the full chunk text; only the excerpt is ever stored (§4.10).
    """

    chunk_id: str
    document_id: UUID
    document_version: int
    collection_id: UUID
    text: str
    document_title: str
    document_role: str
    language: str
    weight: Decimal = Decimal("1.00")
    heading_path: str | None = None
    clause_ref: str | None = None
    parent_chunk_id: str | None = None
    source_half: str = "dense"
    fused_score: float = 0.0
    rerank_score: float | None = None
    excerpt: str = ""
