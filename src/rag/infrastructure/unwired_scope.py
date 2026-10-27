"""Fail-closed placeholder for ``ScopeAuthorizer`` until ``corpusscope`` is wired (slice 3)."""

from __future__ import annotations

from rag.application.shared.scope import ScopeTarget
from rag.domain.shared.enums import GrantLevel
from rag.domain.shared.errors import DomainError, ErrorCode
from rag.domain.shared.identity import RequestIdentity


class UnwiredScopeAuthorizer:
    """Refuses every read with ``503 SCOPE_STORE_UNAVAILABLE``.

    Authorization must never default open. The ``corpusscope``-backed authorizer replaces this
    in ``presentation.generation_dependencies.build_generation_services``.
    """

    async def require_level(
        self, identity: RequestIdentity, target: ScopeTarget, level: GrantLevel
    ) -> None:
        """Always raises."""
        raise DomainError(ErrorCode.SCOPE_STORE_UNAVAILABLE, "Scope resolution is not configured.")
