"""SQL seeding of a complete artefact graph for repository tests (one tender's worth)."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Any
from uuid import UUID, uuid4

type Conn = Any

USER = UUID("0a3f0000-0000-7000-8000-000000000002")


@dataclass
class Graph:
    """Ids of one seeded artefact and everything that hangs from it."""

    tenant: UUID
    tender: UUID | None
    artefact: UUID
    run: UUID
    retrieval_set: UUID
    section: UUID
    section_version: UUID
    version: UUID
    report: UUID
    collection: UUID
    document: UUID
    content: bytes


async def retrieval_set(
    c: Conn, tenant: UUID, tender: UUID | None, artefact: UUID | None = None
) -> UUID:
    """A sufficient retrieval set with two chunks over one collection and document."""
    rs: UUID = await c.fetchval(
        "INSERT INTO retrieval_sets (tenant_id, tender_id, artefact_id, verdict, "
        "scope_resolution_hash, grant_level_applied, filter_set, index_generations, "
        "profile_source, applied_profile, reranker_ran, budget_allocation, "
        "domain_profile_version, language_pack_version) "
        "VALUES ($1, $2, $3, 'sufficient', 'sha256:1c', 'answer', '{}'::jsonb, '{}'::jsonb, "
        "'section_type', '{}'::jsonb, true, '{}'::jsonb, 'dp-1', 'lp-1') RETURNING id",
        tenant,
        tender,
        artefact,
    )
    return rs


async def chunks(c: Conn, tenant: UUID, rs: UUID, collection: UUID, document: UUID) -> None:
    """Two ranked chunks of ``document`` version 1."""
    for rank in (1, 2):
        await c.execute(
            "INSERT INTO retrieval_set_chunks (retrieval_set_id, tenant_id, rank, chunk_id, "
            "document_id, document_version, collection_id, collection_weight, fused_score, "
            "source_half, document_title, document_role, language, excerpt) "
            "VALUES ($1, $2, $3, $4, $5, 1, $6, 0.90, 0.03, 'both', 'Doc', 'specification', "
            "'en', 'excerpt')",
            rs,
            tenant,
            rank,
            f"{document}:{rank}",
            document,
            collection,
        )


async def graph(
    c: Conn,
    tenant: UUID,
    tender: UUID | None,
    *,
    kind: str = "document",
    content: bytes = b"generated text",
) -> Graph:
    """Artefact + run + set + chunks + section + section version + reports + version, linked."""
    collection, document = uuid4(), uuid4()
    art: UUID = await c.fetchval(
        "INSERT INTO artefacts (tenant_id, tender_id, kind, capability, created_by, status) "
        "VALUES ($1, $2, $3::artefact_kind, 'technical_proposal', $4, 'generating') RETURNING id",
        tenant,
        tender,
        kind,
        USER,
    )
    run: UUID = await c.fetchval(
        "INSERT INTO generation_runs (artefact_id, tenant_id, tender_id, principal_id, "
        "requested_mode, effective_mode, eligibility_state, domain_profile_version, "
        "language_pack_version) VALUES ($1, $2, $3, $4, 'assistance', 'assistance', "
        "'GENERATABLE', 'dp-1', 'lp-1') RETURNING id",
        art,
        tenant,
        tender,
        USER,
    )
    rs = await retrieval_set(c, tenant, tender, art)
    await chunks(c, tenant, rs, collection, document)
    sec: UUID = await c.fetchval(
        "INSERT INTO artefact_sections (artefact_id, tenant_id, tender_section_ref, section_type, "
        "ordinal) VALUES ($1, $2, $3, 'introduction', 1) RETURNING id",
        art,
        tenant,
        None if kind == "answer" else "sec-1",
    )
    sv: UUID = await c.fetchval(
        "INSERT INTO artefact_section_versions (section_id, tenant_id, version_no, content_ref, "
        "content_sha256, retrieval_set_id, evidence_tier, completeness, model_profile_id, "
        "prompt_hash, serving_tier) VALUES ($1, $2, 1, $3, $4, $5, 'PARTIAL', 'partial', "
        "'gen-large', 'sha256:aa', 'primary') RETURNING id",
        sec,
        tenant,
        f"tenants/{tenant}/artefacts/{art}/sections/{sec}/v1.md",
        hashlib.sha256(content).digest(),
        rs,
    )
    report: UUID = await c.fetchval(
        "INSERT INTO groundedness_reports (section_version_id, tenant_id, tiers_run, "
        "claims_total, supported, unsupported, miscited, gaps, score, evidence_tier) "
        "VALUES ($1, $2, '{1,2}', 3, 1, 1, 1, 0, 0.500, 'PARTIAL') RETURNING id",
        sv,
        tenant,
    )
    await c.execute(
        "INSERT INTO claim_verifications (report_id, claim_ordinal, tenant_id, outcome, "
        "cited_chunk_ids, corrected_chunk_ids, tier_decided) VALUES "
        "($1, 1, $2, 'supported', '{a:1}', NULL, 1), ($1, 2, $2, 'miscited', '{a:2}', '{b:1}', 1)",
        report,
        tenant,
    )
    await c.execute(
        "INSERT INTO completeness_checks (section_version_id, tenant_id, verdict, items_total, "
        "items_addressed, missed_items) VALUES ($1, $2, 'partial', 9, 7, '{retention}')",
        sv,
        tenant,
    )
    ver: UUID = await c.fetchval(
        "INSERT INTO artefact_versions (artefact_id, tenant_id, version_no, minted_reason, "
        "evidence_tier, completeness, eligibility_state, requested_mode, effective_mode, "
        "domain_profile_version, language_pack_version, model_profile_id, prompt_hash, "
        "parser_version, chunker_version, generation_run_id, delivery) VALUES ($1, $2, 1, "
        "'completion', 'PARTIAL', 'partial', 'GENERATABLE', 'assistance', 'assistance', 'dp-1', "
        "'lp-1', 'gen-large', 'sha256:aa', 'p-1', 'c-1', $3, 'delivered') RETURNING id",
        art,
        tenant,
        run,
    )
    await c.execute(
        "INSERT INTO artefact_version_sections (artefact_version_id, section_version_id, "
        "tenant_id) VALUES ($1, $2, $3)",
        ver,
        sv,
        tenant,
    )
    await c.execute("UPDATE artefact_sections SET state = 'generating' WHERE id = $1", sec)
    await c.execute(
        "UPDATE artefact_sections SET state = 'complete', current_version_id = $2 WHERE id = $1",
        sec,
        sv,
    )
    await c.execute(
        "UPDATE artefacts SET status = 'complete', current_version_id = $2 WHERE id = $1", art, ver
    )
    return Graph(tenant, tender, art, run, rs, sec, sv, ver, report, collection, document, content)


async def facts_and_refinements(c: Conn, g: Graph) -> UUID:
    """A fact, a conflict, one query and one instruction refinement; returns the conflict id."""
    await c.execute(
        "INSERT INTO document_facts (artefact_id, tenant_id, fact_key, value, "
        "established_by_section_id) VALUES ($1, $2, 'access_method', 'crane', $3)",
        g.artefact,
        g.tenant,
        g.section,
    )
    conflict: UUID = await c.fetchval(
        "INSERT INTO fact_conflicts (tenant_id, artefact_id, section_id, fact_key, "
        "established_value, instruction_value, established_by_section_id) "
        "VALUES ($1, $2, $3, 'access_method', 'crane', 'ladder', $3) RETURNING id",
        g.tenant,
        g.artefact,
        g.section,
    )
    await c.execute(
        "INSERT INTO query_refinements (tenant_id, retrieval_set_id, principal_hash, "
        "original_query, pack_version, refiner_version, latency_ms, outcome) "
        "VALUES ($1, $2, 'h', 'q', 'p-1', 'r-1', 12, 'success')",
        g.tenant,
        g.retrieval_set,
    )
    await c.execute(
        "INSERT INTO instruction_refinements (tenant_id, artefact_version_id, section_id, "
        "principal_hash, original_instruction, fact_conflict_id, pack_version, refiner_version, "
        "latency_ms, outcome) VALUES ($1, $2, $3, 'h', 'tighten', $4, 'p-1', 'r-1', 5, 'degraded')",
        g.tenant,
        g.version,
        g.section,
        conflict,
    )
    return conflict


async def checkpoint(c: Conn, g: Graph) -> None:
    """A graph checkpoint (written only by ``rag_activity``)."""
    await c.execute(
        "INSERT INTO graph_checkpoints (tenant_id, section_id, activity_run_id, state) "
        "VALUES ($1, $2, 'act-1', '{}'::jsonb)",
        g.tenant,
        g.section,
    )
