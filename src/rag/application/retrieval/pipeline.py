"""The retrieval pipeline (§6.1 stages 0-9).

Scope, packs, refine, plan, search, fuse, rerank, assemble, judge. Reused unchanged by the
generation routes through ``RetrievalPipeline.run``.
"""

from __future__ import annotations

import asyncio
import hashlib
from dataclasses import dataclass, replace
from decimal import Decimal

from rag.application.packs.ports import PackResolver
from rag.application.packs.resolve import resolve_or_default
from rag.application.retrieval.cache_codec import outcome_from_json, outcome_to_json
from rag.application.retrieval.dtos import PipelineRequest, RetrievalOutcome
from rag.application.retrieval.ports import (
    DenseSearch,
    KeyValueCache,
    LexicalSearch,
    NullObserver,
    PipelineObserver,
    SearchHalfError,
)
from rag.application.retrieval.refiner import QueryRefiner, RefineResult
from rag.application.retrieval.required_input import RequiredInputChecker
from rag.application.retrieval.scope import ScopeProvider
from rag.application.shared.gateway import (
    GatewayContext,
    GatewayDeadlineExceeded,
    GatewayError,
    ModelGateway,
    TokenCounter,
)
from rag.domain.packs.profile import PackBundle, ProfileSource, SectionTypeProfile
from rag.domain.retrieval.budget import (
    Protection,
    candidate_depth,
    plan_budget,
    trim_to_budget,
)
from rag.domain.retrieval.candidate import Candidate
from rag.domain.retrieval.fusion import RankedList, rrf_fuse
from rag.domain.retrieval.plan import DegradationMarker, RetrievalPlan, StructuralFilters
from rag.domain.retrieval.scope import ResolvedScope
from rag.domain.retrieval.sufficiency import judge
from rag.domain.retrieval.text import normalise_query, snap_excerpt
from rag.domain.shared.enums import RetrievalVerdict
from rag.domain.shared.errors import DomainError, ErrorCode
from rag.domain.shared.identity import RequestIdentity

CACHE_TTL_SECONDS = 900
EMBED_TTL_SECONDS = 86400
MAX_WIDENED_DEPTH = 100
CHARS_PER_TOKEN = 4  # passage truncation for the reranker's sequence cap only


@dataclass(frozen=True, slots=True)
class PipelineConfig:
    """Pipeline tunables: LLD §17 values plus the retrieval slice's design defaults."""

    rrf_k: int = 60
    rerank_deadline_seconds: float = 0.09
    rerank_max_in: int = 20
    rerank_max_seq_tokens: int = 256
    context_window_tokens: int = 128_000
    output_reserve_fraction: float = 0.25
    user_input_max_fraction: float = 0.10
    mean_chunk_tokens: int = 350
    candidate_overfetch: float = 1.5
    excerpt_chars: int = 240
    embedding_profile: str = "embed-default"


def _digest(*parts: str) -> str:
    return hashlib.sha256("|".join(parts).encode()).hexdigest()[:20]


class RetrievalPipeline:
    """Runs one retrieval to its three-valued verdict.

    Raises ``DomainError`` for stage 0 (404/403/503), ``RETRIEVAL_FAILED`` when both search
    halves fail, and ``REQUIRED_INPUT_MISSING`` when asked to enforce a blocking gap.
    """

    def __init__(
        self,
        scope: ScopeProvider,
        packs: PackResolver,
        gateway: ModelGateway,
        tokens: TokenCounter,
        dense: DenseSearch,
        lexical: LexicalSearch,
        refiner: QueryRefiner,
        required: RequiredInputChecker,
        cache: KeyValueCache | None,
        config: PipelineConfig,
        observer: PipelineObserver | None = None,
    ) -> None:
        self._scope, self._packs, self._gw, self._tokens = scope, packs, gateway, tokens
        self._dense, self._lexical, self._refiner, self._required = (
            dense,
            lexical,
            refiner,
            required,
        )
        self._cache, self._cfg, self._obs = cache, config, observer or NullObserver()

    # --- the stages ------------------------------------------------------------------------

    async def run(self, identity: RequestIdentity, req: PipelineRequest) -> RetrievalOutcome:
        """Resolve scope and packs, retrieve, and retry once widened on ``insufficient`` (D-16)."""
        obs = self._obs
        async with obs.stage("scope"):
            try:
                scope = await self._scope.resolve(identity, req.scope, req.required_level)
            except DomainError as exc:
                if exc.code is ErrorCode.COLLECTION_NOT_FOUND:
                    obs.scope_empty()
                raise
        ctx = GatewayContext(
            identity.plan, identity.residency_mode.value, tenant_id=identity.tenant_id
        )
        async with obs.stage("pack"):
            bundle, markers = await resolve_or_default(
                self._packs,
                identity.tenant_id,
                obs,
                domain_profile_ref=req.domain_profile_ref,
                language=req.language,
                pinned_version=req.pinned_pack_version,
            )
        profile, source = bundle.domain_profile.profile_for(req.section_type)
        if bundle.is_platform_default:
            source = ProfileSource.PLATFORM_DEFAULT

        # Only a pack-declared section type is a metric label; client input never is (OBS-1).
        label = req.section_type if source is ProfileSource.SECTION_TYPE else None
        key = self._cache_key(scope, req, bundle)
        cached = await self._cache_get(key, scope)
        if cached is not None and not markers:
            obs.retrieval_cache(True)
            obs.verdict(cached.verdict.value, label)
            return cached
        obs.retrieval_cache(False)

        async with obs.stage("refine"):
            refined = await self._refiner.refine(
                tenant_id=identity.tenant_id,
                principal_id=identity.principal_id,
                query=req.query,
                bundle=bundle,
                section_type=req.section_type,
                ctx=ctx,
            )
        pre = [*markers, *(refined.markers if refined else ())]
        async with obs.stage("plan"):
            plan = await self._plan(req, bundle, profile, source, refined, ctx)
        obs.candidate_depth(source.value, plan.candidate_depth)
        if req.enforce_required_inputs and req.section_type:
            gap = await self._required.check(
                scope, bundle.domain_profile.rule_for(req.section_type)
            )
            if gap.blocking:
                raise DomainError(
                    ErrorCode.REQUIRED_INPUT_MISSING,
                    "The scope lacks inputs the section requires.",
                    extensions={
                        "missingRoles": list(gap.missing_roles),
                        "unresolvedEntities": list(gap.unresolved_entities),
                        "emptyCollections": [str(c) for c in gap.empty_collections],
                    },
                )

        first = await self._attempt(scope, req, plan, bundle, profile, refined, pre, ctx)
        final = first
        if first.verdict is RetrievalVerdict.INSUFFICIENT and scope.searchable:
            obs.widened_retry()  # one retry: deeper, role preference relaxed, scope never widened
            widened = replace(
                plan,
                candidate_depth=min(plan.candidate_depth * 2, MAX_WIDENED_DEPTH),
                filters=replace(plan.filters, document_roles=()),
                refiner_roles=(),
            )
            second = await self._attempt(scope, req, widened, bundle, profile, refined, pre, ctx)
            final = replace(second, widened_retry=True, first_attempt=first)
        obs.verdict(final.verdict.value, label)
        if not final.markers:
            await self._cache_put(key, final)
        return final

    async def _plan(
        self,
        req: PipelineRequest,
        bundle: PackBundle,
        profile: SectionTypeProfile,
        source: ProfileSource,
        refined: RefineResult | None,
        ctx: GatewayContext,
    ) -> RetrievalPlan:
        """Stage 3: the budget is resolved here, before the fetch (§6.7, refining MD-26)."""
        cfg = self._cfg
        user_tokens = (await self._tokens.count([req.query], ctx=ctx))[0]
        budget = plan_budget(
            cfg.context_window_tokens,
            output_fraction=cfg.output_reserve_fraction,
            user_input_max_fraction=cfg.user_input_max_fraction,
            skeleton_tokens=bundle.domain_profile.skeleton_tokens,
            user_input_tokens=user_tokens,
        )
        depth = candidate_depth(
            profile.candidate_depth, budget, cfg.mean_chunk_tokens, cfg.candidate_overfetch
        )
        rerank_in = max(1, min(profile.rerank_in, cfg.rerank_max_in, depth))
        rerank_out = max(1, min(profile.rerank_out, rerank_in))
        use = refined.refinement if refined and refined.usable else None
        advised = tuple(
            r for r in (use.preferred_roles if use else ()) if r not in profile.preferred_roles
        )
        queries = (req.query, *([use.refined_query] if use and use.refined_query else []))
        return RetrievalPlan(
            queries=queries,
            filters=StructuralFilters(tuple(profile.preferred_roles), req.language),
            refiner_roles=advised,
            candidate_depth=depth,
            rerank_in=rerank_in,
            rerank_out=rerank_out,
            diversity_bound=profile.diversity_bound,
            profile_source=source,
            values_provisional=profile.values_provisional,
            budget=budget,
            extra={
                "exact_terms": list(use.exact_terms) if use else [],
                "intent": use.intent if use else None,
                "named_collection_ids": [str(c) for c in req.scope.collection_ids],
            },
        )

    async def _embed(
        self, text: str, tenant: str, pack_version: str, ctx: GatewayContext
    ) -> list[float]:
        key = f"{tenant}:qemb:{pack_version}:{self._cfg.embedding_profile}:{normalise_query(text)}"
        if self._cache:
            hit = await self._cache.get(key)
            if hit:
                try:
                    return [float(x) for x in hit.split(",")]
                except ValueError:
                    pass
        vectors = await self._gw.embed([text], ctx=ctx)
        if len(vectors) != 1 or not vectors[0]:
            raise GatewayError("embed returned no vector")
        if self._cache:
            await self._cache.set(key, ",".join(repr(x) for x in vectors[0]), EMBED_TTL_SECONDS)
        return vectors[0]

    async def _attempt(
        self,
        scope: ResolvedScope,
        req: PipelineRequest,
        plan: RetrievalPlan,
        bundle: PackBundle,
        profile: SectionTypeProfile,
        refined: RefineResult | None,
        pre: list[DegradationMarker],
        ctx: GatewayContext,
    ) -> RetrievalOutcome:
        """Stages 4-9 once. Markers: ``pre`` plus what this attempt degrades."""
        obs = self._obs
        markers = list(pre)
        use = refined.refinement if refined and refined.usable else None
        base = RetrievalOutcome(
            scope=scope,
            verdict=RetrievalVerdict.INSUFFICIENT,
            verdict_reason=None,
            chunks=(),
            markers=(),
            plan=plan,
            reranker_ran=False,
            domain_profile_version=bundle.domain_profile.version,
            language_pack_version=bundle.language_pack.version,
            refinement=refined.refinement if refined else None,
        )
        if not scope.searchable:  # SCOPE-12: every collection is an empty corpus
            return replace(base, verdict_reason="no_searchable_collection", markers=tuple(markers))

        original, depth = req.query, plan.candidate_depth
        searched = (use.refined_query if use and use.refined_query else None) or original

        # RET-2: refiner-advised roles never narrow the original query's own lexical list.
        refined_filters = replace(
            plan.filters,
            document_roles=tuple(
                dict.fromkeys([*plan.filters.document_roles, *plan.refiner_roles])
            ),
        )
        dense_filters = refined_filters if use else plan.filters

        async def dense_half() -> list[Candidate]:
            vector = await self._embed(
                searched, str(scope.tenant_id), bundle.domain_profile.version, ctx
            )
            return await self._dense.search(scope, vector, limit=depth, filters=dense_filters)

        lexical_queries = [("lexical_original", original)]
        if use and use.refined_query and use.refined_query != original:
            lexical_queries.append(("lexical_refined", use.refined_query))
        if use and use.exact_terms:  # exact terms bypass refinement: phrase predicates
            lexical_queries.append(
                ("lexical_exact", " ".join(f'"{t}"' for t in use.exact_terms if '"' not in t))
            )
        async with obs.stage("search"):
            results = await asyncio.gather(
                dense_half(),
                *(
                    self._lexical.search(
                        scope,
                        q,
                        limit=depth,
                        filters=plan.filters if name == "lexical_original" else refined_filters,
                    )
                    for name, q in lexical_queries
                ),
                return_exceptions=True,
            )
        dense_res, lexical_res = results[0], results[1:]
        lists: list[RankedList] = []
        shift = -(self._cfg.rrf_k // 6) if use and use.intent == "reference" else 0
        dense_ok = not isinstance(dense_res, BaseException)
        if isinstance(dense_res, GatewayError):
            markers.append(DegradationMarker("embed", dense_res.reason, "lexical_only"))
            obs.marker("embed", dense_res.reason)
        elif isinstance(dense_res, SearchHalfError):
            markers.append(DegradationMarker("dense_search", "unavailable", "lexical_only"))
            obs.marker("dense_search", "unavailable")
        elif isinstance(dense_res, BaseException):
            raise dense_res
        else:
            lists.append(RankedList("dense", dense_res))
        lexical_ok = False
        for (name, _), res in zip(lexical_queries, lexical_res, strict=True):
            if isinstance(res, SearchHalfError):
                continue
            if isinstance(res, BaseException):
                raise res
            lexical_ok = True
            lists.append(RankedList(name, res, shift))
        if not lexical_ok:
            if not dense_ok:
                raise DomainError(ErrorCode.RETRIEVAL_FAILED, "Both search halves are unavailable.")
            markers.append(DegradationMarker("lexical_search", "unavailable", "dense_only"))
            obs.marker("lexical_search", "unavailable")

        async with obs.stage("fusion"):
            fused = rrf_fuse(lists, self._cfg.rrf_k)
        ranked, reranked = await self._rerank(original, fused, plan, markers, ctx)

        async with obs.stage("assembly"):
            outcome = await self._assemble(
                base, ranked, reranked, plan, bundle, profile, req, markers, ctx
            )
        return outcome

    async def _rerank(
        self,
        query: str,
        fused: list[Candidate],
        plan: RetrievalPlan,
        markers: list[DegradationMarker],
        ctx: GatewayContext,
    ) -> tuple[list[Candidate], bool]:
        """Stage 7: 90 ms, no retry. Any failure keeps the fused order with a marker."""
        top = fused[: plan.rerank_in]
        if not top:
            return [], False
        cap = self._cfg.rerank_max_seq_tokens * CHARS_PER_TOKEN
        async with self._obs.stage("rerank"):
            try:
                scores = await asyncio.wait_for(
                    self._gw.rerank(
                        query,
                        [c.text[:cap] for c in top],
                        ctx=ctx,
                        deadline_seconds=self._cfg.rerank_deadline_seconds,
                    ),
                    self._cfg.rerank_deadline_seconds,
                )
                if len(scores) != len(top):
                    raise GatewayError("rerank returned the wrong number of scores")
            except (GatewayError, TimeoutError) as exc:
                err = GatewayDeadlineExceeded() if isinstance(exc, TimeoutError) else exc
                reason = err.reason if isinstance(err, GatewayError) else "unavailable"
                markers.append(DegradationMarker("rerank", reason, "fused_order"))
                self._obs.marker("rerank", reason)
                self._obs.rerank_skipped(reason)
                return top[: plan.rerank_out], False
        scored = [replace(c, rerank_score=s) for c, s in zip(top, scores, strict=True)]
        scored.sort(key=lambda c: (-(c.rerank_score or 0.0), -c.fused_score, c.chunk_id))
        return scored[: plan.rerank_out], True

    async def _assemble(
        self,
        base: RetrievalOutcome,
        ranked: list[Candidate],
        reranked: bool,
        plan: RetrievalPlan,
        bundle: PackBundle,
        profile: SectionTypeProfile,
        req: PipelineRequest,
        markers: list[DegradationMarker],
        ctx: GatewayContext,
    ) -> RetrievalOutcome:
        """Stages 8-9: trim to the section budget, snap excerpts, judge sufficiency."""
        rule = bundle.domain_profile.rule_for(req.section_type) if req.section_type else None
        protection = Protection(
            frozenset(rule.document_roles) if rule else frozenset(),
            profile.mandatory_clauses,
        )
        counts = await self._tokens.count([c.text for c in ranked], ctx=ctx) if ranked else []
        trim = trim_to_budget(
            ranked,
            counts,
            plan.budget.evidence_tokens,
            diversity_bound=plan.diversity_bound,
            protection=protection,
        )
        limit = min(req.limit or plan.rerank_out, plan.rerank_out)
        kept = list(trim.kept)[:limit] if trim.refused is None else list(ranked)[:limit]
        chunks = tuple(
            replace(c, excerpt=snap_excerpt(c.text, self._cfg.excerpt_chars)) for c in kept
        )
        verdict, reason = RetrievalVerdict.INSUFFICIENT, trim.refused
        if trim.refused is None:
            j = judge([c.rerank_score for c in chunks], profile.sufficiency, reranked=reranked)
            verdict, reason = j.verdict, j.reason
        return replace(
            base,
            verdict=verdict,
            verdict_reason=reason,
            chunks=chunks,
            markers=tuple(markers),
            reranker_ran=reranked,
        )

    # --- the retrieval-set cache (§11.1, §11.3) --------------------------------------------

    def _cache_key(self, scope: ResolvedScope, req: PipelineRequest, bundle: PackBundle) -> str:
        packs = f"{bundle.domain_profile.version}+{bundle.language_pack.version}"
        epochs = _digest(scope.generation_epoch_digest())  # generation and purge epoch (LD-16)
        shape = _digest(
            req.section_type or "-", str(req.limit), req.language or "-", str(req.required_level)
        )
        return (
            f"{scope.tenant_id}:rset:{scope.resolution_hash[:20]}:{epochs}:"
            f"{_digest(normalise_query(req.query))}:{packs}:{shape}"
        )

    async def _cache_get(self, key: str, scope: ResolvedScope) -> RetrievalOutcome | None:
        if self._cache is None:
            return None
        raw = await self._cache.get(key)
        if raw is None:
            return None
        try:
            return outcome_from_json(raw, scope)
        except (ValueError, KeyError, TypeError):
            return None

    async def _cache_put(self, key: str, outcome: RetrievalOutcome) -> None:
        if self._cache is not None:
            await self._cache.set(key, outcome_to_json(outcome), CACHE_TTL_SECONDS)


def to_decimal(value: float) -> Decimal:
    """A float score as the exact ``numeric`` the record stores."""
    return Decimal(repr(value))
