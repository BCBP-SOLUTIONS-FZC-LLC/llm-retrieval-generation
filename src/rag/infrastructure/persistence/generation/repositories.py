"""asyncpg implementations of the generation repository ports.

Every statement runs on the connection ``run_in_tx`` hands over, inside the tenant's RLS
(``FORCE ROW LEVEL SECURITY``, GUC bound by the pool). Statements therefore carry no
``tenant_id`` predicate for reads: another tenant's row is simply not there.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any
from uuid import UUID

from rag.domain.generation.artefact import Artefact, ArtefactVersion
from rag.domain.generation.audit import AuditEntry, AuditEntryDraft
from rag.domain.generation.refinement import QueryRefinement
from rag.domain.generation.retrieval import RetrievalSetView
from rag.domain.generation.views import (
    ArtefactView,
    SectionStateView,
    SectionVersionRecord,
    VersionDetail,
    VersionSectionDetail,
)
from rag.domain.shared.enums import EvidenceTier
from rag.infrastructure.persistence.generation import rows
from rag.infrastructure.persistence.generation.rows import dump_json

type Conn = Any


class PgArtefactReadRepository:
    """``ArtefactReadRepository`` over ``artefacts`` and friends."""

    async def get_artefact(self, tx: Conn, artefact_id: UUID) -> Artefact | None:
        """The artefact row."""
        r = await tx.fetchrow("SELECT * FROM artefacts WHERE id = $1", artefact_id)
        return None if r is None else rows.artefact(r)

    async def get_artefact_view(self, tx: Conn, artefact_id: UUID) -> ArtefactView | None:
        """Artefact, current version, and the latest run id (ART-3: derived, never stored)."""
        artefact = await self.get_artefact(tx, artefact_id)
        if artefact is None:
            return None
        version = None
        if artefact.current_version_id is not None:
            v = await tx.fetchrow(
                "SELECT * FROM artefact_versions WHERE id = $1", artefact.current_version_id
            )
            version = None if v is None else rows.artefact_version(v)
        run_id = await tx.fetchval(
            "SELECT id FROM generation_runs WHERE artefact_id = $1 "
            "ORDER BY created_at DESC, id DESC LIMIT 1",
            artefact_id,
        )
        return ArtefactView(artefact, version, run_id)

    async def list_versions(
        self, tx: Conn, artefact_id: UUID, *, limit: int, offset: int
    ) -> tuple[list[ArtefactVersion], int]:
        """Versions newest first (``idx_av_tenant``) and the total."""
        found = await tx.fetch(
            "SELECT * FROM artefact_versions WHERE artefact_id = $1 "
            "ORDER BY version_no DESC LIMIT $2 OFFSET $3",
            artefact_id,
            limit,
            offset,
        )
        total = await tx.fetchval(
            "SELECT count(*) FROM artefact_versions WHERE artefact_id = $1", artefact_id
        )
        return [rows.artefact_version(r) for r in found], int(total)

    async def get_version_detail(
        self, tx: Conn, artefact_id: UUID, version_no: int
    ) -> VersionDetail | None:
        """A version with the per-section verification records it comprised (AVS-2)."""
        v = await tx.fetchrow(
            "SELECT * FROM artefact_versions WHERE artefact_id = $1 AND version_no = $2",
            artefact_id,
            version_no,
        )
        if v is None:
            return None
        version = rows.artefact_version(v)
        members = await tx.fetch(
            "SELECT s.id AS s_id, sv.id AS sv_id FROM artefact_version_sections avs "
            "JOIN artefact_section_versions sv ON sv.id = avs.section_version_id "
            "JOIN artefact_sections s ON s.id = sv.section_id "
            "WHERE avs.artefact_version_id = $1 ORDER BY s.ordinal",
            version.id,
        )
        sv_ids = [m["sv_id"] for m in members]
        s_ids = [m["s_id"] for m in members]
        sections = {
            r["id"]: rows.section(r)
            for r in await tx.fetch("SELECT * FROM artefact_sections WHERE id = ANY($1)", s_ids)
        }
        versions = {
            r["id"]: rows.section_version(r)
            for r in await tx.fetch(
                "SELECT * FROM artefact_section_versions WHERE id = ANY($1)", sv_ids
            )
        }
        reports = {
            r["section_version_id"]: rows.groundedness(r)
            for r in await tx.fetch(
                "SELECT * FROM groundedness_reports WHERE section_version_id = ANY($1)", sv_ids
            )
        }
        checks = {
            r["section_version_id"]: rows.completeness(r)
            for r in await tx.fetch(
                "SELECT * FROM completeness_checks WHERE section_version_id = ANY($1)", sv_ids
            )
        }
        claims: dict[UUID, list[Any]] = {}
        for r in await tx.fetch(
            "SELECT * FROM claim_verifications WHERE report_id = ANY($1) ORDER BY claim_ordinal",
            [g.id for g in reports.values()],
        ):
            claims.setdefault(r["report_id"], []).append(rows.claim(r))
        details: list[VersionSectionDetail] = []
        for m in members:
            report = reports.get(m["sv_id"])
            details.append(
                VersionSectionDetail(
                    section=sections[m["s_id"]],
                    section_version=versions[m["sv_id"]],
                    groundedness=report,
                    claims=tuple(claims.get(report.id, ())) if report else (),
                    completeness=checks.get(m["sv_id"]),
                )
            )
        return VersionDetail(version, tuple(details))

    async def list_sections(
        self, tx: Conn, artefact_id: UUID, *, limit: int, offset: int
    ) -> tuple[list[SectionStateView], int]:
        """Sections by ordinal with the current version's tier (``idx_a4_read``)."""
        found = await tx.fetch(
            "SELECT s.*, sv.evidence_tier AS current_tier FROM artefact_sections s "
            "LEFT JOIN artefact_section_versions sv ON sv.id = s.current_version_id "
            "WHERE s.artefact_id = $1 ORDER BY s.ordinal LIMIT $2 OFFSET $3",
            artefact_id,
            limit,
            offset,
        )
        total = await tx.fetchval(
            "SELECT count(*) FROM artefact_sections WHERE artefact_id = $1", artefact_id
        )
        return [
            SectionStateView(
                rows.section(r),
                None if r["current_tier"] is None else EvidenceTier(r["current_tier"]),
            )
            for r in found
        ], int(total)

    async def get_section_version_record(
        self, tx: Conn, artefact_id: UUID, section_ref: str, version_no: int
    ) -> SectionVersionRecord | None:
        """The section whose ``tender_section_ref`` (or id) is ``section_ref``, and its version."""
        s = await tx.fetchrow(
            "SELECT * FROM artefact_sections WHERE artefact_id = $1 "
            "AND (tender_section_ref = $2 OR id::text = $2) ORDER BY ordinal LIMIT 1",
            artefact_id,
            section_ref,
        )
        if s is None:
            return None
        sv = await tx.fetchrow(
            "SELECT * FROM artefact_section_versions WHERE section_id = $1 AND version_no = $2",
            s["id"],
            version_no,
        )
        if sv is None:
            return None
        return SectionVersionRecord(rows.section(s), rows.section_version(sv))


class PgRetrievalSetRepository:
    """``RetrievalSetRepository`` over ``retrieval_sets`` / ``_chunks`` / ``_purges``."""

    async def get_view(self, tx: Conn, retrieval_set_id: UUID) -> RetrievalSetView | None:
        """The set, its chunks in rank order, and its purge rows oldest first."""
        s = await tx.fetchrow("SELECT * FROM retrieval_sets WHERE id = $1", retrieval_set_id)
        if s is None:
            return None
        chunks = await tx.fetch(
            "SELECT * FROM retrieval_set_chunks WHERE retrieval_set_id = $1 ORDER BY rank",
            retrieval_set_id,
        )
        purges = await tx.fetch(
            "SELECT * FROM retrieval_set_purges WHERE retrieval_set_id = $1 ORDER BY purged_at, id",
            retrieval_set_id,
        )
        return RetrievalSetView(
            rows.retrieval_set(s),
            tuple(rows.retrieval_chunk(c) for c in chunks),
            tuple(rows.retrieval_purge(p) for p in purges),
        )

    async def insert(self, tx: Conn, view: RetrievalSetView) -> None:
        """Insert the set and its chunks (RS-1: written once, in the caller's transaction)."""
        s = view.retrieval_set
        await tx.execute(
            "INSERT INTO retrieval_sets (id, tenant_id, tender_id, artefact_id, verdict, "
            "verdict_reason, scope_resolution_hash, grant_level_applied, grant_level_clamped, "
            "filter_set, index_generations, profile_source, applied_profile, values_provisional, "
            "reranker_ran, budget_allocation, query_refinement_id, widened_retry_of, "
            "domain_profile_version, language_pack_version, degradation_markers) "
            "VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10::jsonb,$11::jsonb,$12,$13::jsonb,$14,$15,"
            "$16::jsonb,$17,$18,$19,$20,$21::jsonb)",
            s.id,
            s.tenant_id,
            s.tender_id,
            s.artefact_id,
            s.verdict.value,
            s.verdict_reason,
            s.scope_resolution_hash,
            s.grant_level_applied.value,
            s.grant_level_clamped,
            dump_json(s.filter_set),
            dump_json(s.index_generations),
            s.profile_source,
            dump_json(s.applied_profile),
            s.values_provisional,
            s.reranker_ran,
            dump_json(s.budget_allocation),
            s.query_refinement_id,
            s.widened_retry_of,
            s.domain_profile_version,
            s.language_pack_version,
            dump_json(s.degradation_markers),
        )
        if view.chunks:
            await tx.executemany(
                "INSERT INTO retrieval_set_chunks (retrieval_set_id, tenant_id, rank, chunk_id, "
                "document_id, document_version, collection_id, collection_weight, fused_score, "
                "rerank_score, source_half, parent_chunk_id, document_title, heading_path, "
                "clause_ref, document_role, language, excerpt, excerpt_is_translation, "
                "source_span) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16,"
                "$17,$18,$19,$20::jsonb)",
                [
                    (
                        c.retrieval_set_id,
                        c.tenant_id,
                        c.rank,
                        c.chunk_id,
                        c.document_id,
                        c.document_version,
                        c.collection_id,
                        c.collection_weight,
                        c.fused_score,
                        c.rerank_score,
                        c.source_half,
                        c.parent_chunk_id,
                        c.document_title,
                        c.heading_path,
                        c.clause_ref,
                        c.document_role,
                        c.language,
                        c.excerpt,
                        c.excerpt_is_translation,
                        None if c.source_span is None else dump_json(c.source_span),
                    )
                    for c in view.chunks
                ],
            )

    async def purge_for_document(
        self,
        tx: Conn,
        *,
        tenant_id: UUID,
        collection_id: UUID,
        document_id: UUID,
        document_version: int,
        event_id: UUID,
    ) -> int:
        """EVT-1: one marker per set whose recorded chunks match the 4-tuple (RSP-1 idempotent)."""
        status: str = await tx.execute(
            "INSERT INTO retrieval_set_purges "
            "(retrieval_set_id, tenant_id, purged_document_id, purged_version, chunk_ids, "
            "event_id) "
            "SELECT c.retrieval_set_id, c.tenant_id, c.document_id, c.document_version, "
            "       array_agg(c.chunk_id ORDER BY c.rank), $5 "
            "FROM retrieval_set_chunks c "
            "WHERE c.tenant_id = $1 AND c.collection_id = $2 AND c.document_id = $3 "
            "  AND c.document_version = $4 "
            "GROUP BY c.retrieval_set_id, c.tenant_id, c.document_id, c.document_version "
            "ON CONFLICT (retrieval_set_id, event_id) DO NOTHING",
            tenant_id,
            collection_id,
            document_id,
            document_version,
            event_id,
        )
        return int(status.rsplit(" ", 1)[-1])


class PgQueryRefinementRepository:
    """``QueryRefinementRepository`` over ``query_refinements``."""

    async def insert(self, tx: Conn, refinement: QueryRefinement) -> None:
        """One row per refiner call (REF-1). ``user_action`` stays at its default ``unknown``."""
        r = refinement
        await tx.execute(
            "INSERT INTO query_refinements (id, tenant_id, retrieval_set_id, principal_hash, "
            "original_query, refined_query, intent, exact_terms, preferred_roles, section_type, "
            "pack_version, refiner_version, latency_ms, outcome) "
            "VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14)",
            r.id,
            r.tenant_id,
            r.retrieval_set_id,
            r.principal_hash,
            r.original_query,
            r.refined_query,
            r.intent,
            list(r.exact_terms),
            list(r.preferred_roles),
            r.section_type,
            r.pack_version,
            r.refiner_version,
            r.latency_ms,
            r.outcome.value,
        )


class PgAuditChainRepository:
    """``AuditChainRepository``: the database allocates seq and the hashes (AC-2, AC-3)."""

    async def append(self, tx: Conn, draft: AuditEntryDraft) -> AuditEntry | None:
        """Insert without ``seq`` or hashes; ``audit_chain_append()`` fills them under the lock."""
        r = await tx.fetchrow(
            "INSERT INTO artefact_audit_chain (tenant_id, occurred_at, entry_type, action, "
            "actor_type, actor_id, artefact_id, target_type, target_id, source_service, "
            "source_event_type, source_event_id, trace_id, metadata) "
            "VALUES ($1, $2, $3, $4, $5::audit_actor_type, $6, $7, $8, $9, $10, $11, $12, $13, "
            "$14::jsonb) "
            "ON CONFLICT (tenant_id, source_event_id) DO NOTHING RETURNING *",
            draft.tenant_id,
            draft.occurred_at,
            draft.entry_type,
            draft.action,
            draft.actor_type.value,
            draft.actor_id,
            draft.artefact_id,
            draft.target_type,
            draft.target_id,
            draft.source_service,
            draft.source_event_type,
            draft.source_event_id,
            draft.trace_id,
            dump_json(draft.metadata),
        )
        return None if r is None else rows.audit_entry(r)

    async def list_chain(
        self, tx: Conn, tenant_id: UUID, *, after_seq: int = 0
    ) -> list[AuditEntry]:
        """One tenant's entries in ``seq`` order."""
        found = await tx.fetch(
            "SELECT * FROM artefact_audit_chain WHERE tenant_id = $1 AND seq > $2 ORDER BY seq",
            tenant_id,
            after_seq,
        )
        return [rows.audit_entry(r) for r in found]


class PgVersionRepository:
    """``VersionRepository``."""

    async def mark_superseded_by_section_edit(
        self, tx: Conn, artefact_id: UUID, section_id: UUID
    ) -> list[UUID]:
        """AV-6: the one column ``trg_version_immutable`` exempts."""
        found = await tx.fetch(
            "UPDATE artefact_versions v SET superseded_by_section_edit = true "
            "WHERE v.artefact_id = $1 AND NOT v.superseded_by_section_edit AND v.id IN ("
            "  SELECT avs.artefact_version_id FROM artefact_version_sections avs "
            "  JOIN artefact_section_versions sv ON sv.id = avs.section_version_id "
            "  WHERE sv.section_id = $2) RETURNING v.id",
            artefact_id,
            section_id,
        )
        return [r["id"] for r in found]


_IN_SECTIONS = "(SELECT id FROM artefact_sections WHERE artefact_id = ANY($1::uuid[]))"
_IN_SECTION_VERSIONS = (
    "(SELECT id FROM artefact_section_versions WHERE section_id IN " + _IN_SECTIONS + ")"
)
_IN_VERSIONS = "(SELECT id FROM artefact_versions WHERE artefact_id = ANY($1::uuid[]))"
_IN_CONFLICTS = "(SELECT id FROM fact_conflicts WHERE artefact_id = ANY($1::uuid[]))"
# $2 is the tender id for a tender purge, the tenant id for a tenant purge (every set of the
# tenant, whatever its age).
_SETS_OF_TENDER = (
    "(SELECT id FROM retrieval_sets WHERE artefact_id = ANY($1::uuid[]) OR tender_id = $2)"
)
_SETS_OF_TENANT = (
    "(SELECT id FROM retrieval_sets WHERE artefact_id = ANY($1::uuid[]) OR tenant_id = $2)"
)


class PgArtefactPurgeRepository:
    """``ArtefactPurgeRepository``: the ART-9 delete, children before parents."""

    async def list_artefacts_for_purge(
        self, tx: Conn, tenant_id: UUID, tender_id: UUID | None
    ) -> list[Artefact]:
        """Artefacts of the tender, or of the tenant (free-standing answers included, ART-8)."""
        if tender_id is None:
            found = await tx.fetch(
                "SELECT * FROM artefacts WHERE tenant_id = $1 ORDER BY id", tenant_id
            )
        else:
            found = await tx.fetch(
                "SELECT * FROM artefacts WHERE tenant_id = $1 AND tender_id = $2 ORDER BY id",
                tenant_id,
                tender_id,
            )
        return [rows.artefact(r) for r in found]

    async def delete_artefact_trees(
        self, tx: Conn, tenant_id: UUID, tender_id: UUID | None, artefact_ids: Sequence[UUID]
    ) -> None:
        """Delete the scope in dependency order with the two cyclic FKs deferred.

        Only ``artefact_versions.generation_run_id``, ``generation_runs.base_version_id`` and the
        two ``current_version_id`` pointers are deferrable (ART-2, AS-3, ART-9); every other
        foreign key is immediate, hence the order. The audit chain has no FK and is not touched.
        """
        ids = list(artefact_ids)
        sets = _SETS_OF_TENANT if tender_id is None else _SETS_OF_TENDER
        key = tenant_id if tender_id is None else tender_id
        sv, vs = _IN_SECTION_VERSIONS, _IN_VERSIONS
        await tx.execute("SET CONSTRAINTS ALL DEFERRED")
        statements: list[tuple[str, tuple[Any, ...]]] = [
            (
                "DELETE FROM claim_verifications WHERE report_id IN (SELECT id FROM "
                f"groundedness_reports WHERE section_version_id IN {sv})",
                (ids,),
            ),
            (f"DELETE FROM groundedness_reports WHERE section_version_id IN {sv}", (ids,)),
            (f"DELETE FROM completeness_checks WHERE section_version_id IN {sv}", (ids,)),
            (
                "DELETE FROM instruction_refinements WHERE "
                f"artefact_version_id IN {_IN_VERSIONS} OR section_id IN {_IN_SECTIONS} "
                f"OR fact_conflict_id IN {_IN_CONFLICTS}",
                (ids,),
            ),
            (f"DELETE FROM query_refinements WHERE retrieval_set_id IN {sets}", (ids, key)),
            (f"DELETE FROM artefact_version_sections WHERE artefact_version_id IN {vs}", (ids,)),
            (
                f"DELETE FROM retrieval_set_purges WHERE retrieval_set_id IN {sets}",
                (ids, key),
            ),
            (
                f"DELETE FROM retrieval_set_chunks WHERE retrieval_set_id IN {sets}",
                (ids, key),
            ),
            (f"DELETE FROM graph_checkpoints WHERE section_id IN {_IN_SECTIONS}", (ids,)),
            ("DELETE FROM document_facts WHERE artefact_id = ANY($1::uuid[])", (ids,)),
            ("DELETE FROM fact_conflicts WHERE artefact_id = ANY($1::uuid[])", (ids,)),
            (
                f"DELETE FROM artefact_section_versions WHERE section_id IN {_IN_SECTIONS}",
                (ids,),
            ),
            ("DELETE FROM artefact_sections WHERE artefact_id = ANY($1::uuid[])", (ids,)),
            (f"DELETE FROM retrieval_sets WHERE id IN {sets}", (ids, key)),
            ("DELETE FROM artefact_versions WHERE artefact_id = ANY($1::uuid[])", (ids,)),
            ("DELETE FROM generation_runs WHERE artefact_id = ANY($1::uuid[])", (ids,)),
            ("DELETE FROM artefacts WHERE id = ANY($1::uuid[])", (ids,)),
        ]
        for sql, args in statements:
            await tx.execute(sql, *args)
