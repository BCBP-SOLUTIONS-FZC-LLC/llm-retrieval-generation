"""Required-input checks (§6.9): the scoped counts behind R-7 and plan construction."""

from __future__ import annotations

import asyncio
from uuid import UUID

from rag.application.packs.ports import PackResolver
from rag.application.packs.resolve import resolve_or_default
from rag.application.retrieval.dtos import RequiredInputCommand, RequiredInputResult
from rag.application.retrieval.ports import (
    NullObserver,
    PipelineObserver,
    PointInspector,
    SearchHalfError,
)
from rag.application.retrieval.scope import ScopeProvider
from rag.domain.retrieval.scope import ResolvedScope
from rag.domain.retrieval.sufficiency import GapReport, RequiredInputRule, evaluate_required_inputs
from rag.domain.shared.enums import GrantLevel
from rag.domain.shared.errors import DomainError, ErrorCode
from rag.domain.shared.identity import RequestIdentity


class RequiredInputChecker:
    """Counts roles and entity types in scope and reduces them to a ``GapReport``.

    Deterministic, no model call (LD-100). A count that cannot run fails the check with
    ``503 RETRIEVAL_FAILED``: it never guesses a gap or a pass.
    """

    def __init__(self, inspector: PointInspector, observer: PipelineObserver | None = None) -> None:
        self._inspector, self._obs = inspector, observer or NullObserver()

    async def check(self, scope: ResolvedScope, rule: RequiredInputRule) -> GapReport:
        """The gap report for ``rule`` against ``scope`` (``is_current`` applies, RET-8)."""
        roles, entities = list(rule.document_roles), list(rule.resolvable_entities)
        try:
            counts = await asyncio.gather(
                *(self._inspector.count(scope, document_role=r) for r in roles),
                *(self._inspector.count(scope, entity_type=e) for e in entities),
            )
        except SearchHalfError as exc:
            raise DomainError(
                ErrorCode.RETRIEVAL_FAILED, "The required-input check could not run."
            ) from exc
        report = evaluate_required_inputs(
            rule,
            role_counts=dict(zip(roles, counts[: len(roles)], strict=True)),
            entity_counts=dict(zip(entities, counts[len(roles) :], strict=True)),
            empty_collections=scope.empty_collection_ids,
        )
        for role in report.missing_roles:
            self._obs.required_input_gap(role)
        return report


class CheckRequiredInputs:
    """R-7: run the pack's rules against the resolved scope without generating (§5.7.7)."""

    def __init__(
        self,
        scope: ScopeProvider,
        packs: PackResolver,
        checker: RequiredInputChecker,
        observer: PipelineObserver | None = None,
    ) -> None:
        self._scope, self._packs, self._checker = scope, packs, checker
        self._obs = observer or NullObserver()

    async def execute(
        self, identity: RequestIdentity, cmd: RequiredInputCommand
    ) -> RequiredInputResult:
        """A gap report per requested section type. A blocking gap is a ``200`` (§5.7.7)."""
        scope = await self._resolve(identity, cmd)
        bundle, _ = await resolve_or_default(self._packs, identity.tenant_id, self._obs)
        profile = bundle.domain_profile
        reports = await asyncio.gather(
            *(self._checker.check(scope, profile.rule_for(s)) for s in cmd.section_types)
        )
        return RequiredInputResult(
            dict(zip(cmd.section_types, reports, strict=True)), profile.version
        )

    async def _resolve(self, identity: RequestIdentity, cmd: RequiredInputCommand) -> ResolvedScope:
        spec = cmd.scope
        tender: UUID | None = cmd.tender_id or spec.tender_id
        merged = type(spec)(tender, spec.collection_ids, spec.collection_types)
        try:
            return await self._scope.resolve(identity, merged, GrantLevel.SEARCH)
        except DomainError as exc:
            if exc.code is ErrorCode.COLLECTION_NOT_FOUND:
                self._obs.scope_empty()
            raise
