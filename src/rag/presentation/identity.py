"""Identity headers the platform library does not carry (API-12) and the auth dependencies.

``fastapicommon`` extracts ``x-tenant-id``, ``x-user-id`` and ``x-tenant-roles`` into its frozen
``RequestContext``. ``x-departments``, ``x-plan``, ``x-feature-flags`` and ``x-residency-mode``
are ours: a pure-ASGI middleware parses them into a request-scoped carrier (a ``ContextVar``).
"""

from __future__ import annotations

from contextvars import ContextVar
from dataclasses import dataclass
from uuid import UUID

from fastapi import Depends
from fastapicommon.auth import require_authenticated_user
from fastapicommon.context import CurrentUser, get_request_context
from fastapicommon.exceptions import UnauthorizedException
from starlette.datastructures import Headers
from starlette.types import ASGIApp, Receive, Scope, Send

from rag.domain.shared.identity import DepartmentRole, RequestIdentity, ResidencyMode

DEPARTMENTS_HEADER = "x-departments"
PLAN_HEADER = "x-plan"
FEATURE_FLAGS_HEADER = "x-feature-flags"
RESIDENCY_MODE_HEADER = "x-residency-mode"


@dataclass(frozen=True, slots=True)
class ExtraIdentity:
    """The four service-owned identity headers, parsed."""

    departments: tuple[DepartmentRole, ...] = ()
    plan: str | None = None
    feature_flags: frozenset[str] = frozenset()
    residency_mode: ResidencyMode = ResidencyMode.STANDARD


_extra_identity: ContextVar[ExtraIdentity] = ContextVar(
    "rag_extra_identity",
    default=ExtraIdentity(),  # noqa: B039 - frozen dataclass
)


def get_extra_identity() -> ExtraIdentity:
    """The current request's extra identity (empty outside a request)."""
    return _extra_identity.get()


def parse_departments(raw: str) -> tuple[DepartmentRole, ...]:
    """Parse ``<department-uuid>:<role>`` pairs, comma-separated (§22.10).

    An authorization input, so it fails closed: a malformed pair is dropped (less scope),
    never guessed at.
    """
    pairs: list[DepartmentRole] = []
    for item in raw.split(","):
        dept, sep, role = item.strip().partition(":")
        if not sep or not role.strip():
            continue
        try:
            pairs.append(DepartmentRole(UUID(dept.strip()), role.strip()))
        except ValueError:
            continue
    return tuple(pairs)


def parse_feature_flags(raw: str) -> frozenset[str]:
    """Parse a plain comma-separated flag list; presence means true (IAM HLD §5.4)."""
    return frozenset(flag.strip() for flag in raw.split(",") if flag.strip())


def parse_residency_mode(raw: str | None) -> ResidencyMode:
    """``standard`` when absent or ``standard``; any other value means ``strict`` (LD-74)."""
    if raw is None or raw.strip().lower() == ResidencyMode.STANDARD:
        return ResidencyMode.STANDARD
    return ResidencyMode.STRICT


def _single(headers: Headers, name: str) -> str | None:
    """The header's value, or ``None`` when absent or repeated (ambiguous)."""
    values = headers.getlist(name)
    return values[0].strip() or None if len(values) == 1 else None


class ExtraIdentityMiddleware:
    """Parses the four extra identity headers into the request-scoped carrier."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Bind the parsed headers for the duration of the request."""
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        headers = Headers(scope=scope)
        extra = ExtraIdentity(
            departments=parse_departments(",".join(headers.getlist(DEPARTMENTS_HEADER))),
            plan=_single(headers, PLAN_HEADER),
            feature_flags=parse_feature_flags(",".join(headers.getlist(FEATURE_FLAGS_HEADER))),
            residency_mode=parse_residency_mode(_single(headers, RESIDENCY_MODE_HEADER)),
        )
        token = _extra_identity.set(extra)
        try:
            await self.app(scope, receive, send)
        finally:
            _extra_identity.reset(token)


def _parse_uuid(value: str | None) -> UUID:
    """The identity header value as a uuid, else 401."""
    try:
        return UUID(value or "")
    except ValueError:
        raise UnauthorizedException("x-tenant-id must be a uuid.") from None


def require_tenant() -> UUID:
    """Dependency for internal routes (LD-95): authenticates by ``x-tenant-id`` alone.

    Internal callers send no ``x-user-id``, which ``require_authenticated_user`` demands.
    Answers ``401 UNAUTHORIZED`` where the header is absent, repeated or not a uuid.
    """
    context = get_request_context()
    if context.identity_rejection is not None or context.tenant_id is None:
        raise UnauthorizedException("x-tenant-id is required.")
    return _parse_uuid(context.tenant_id)


def require_identity(user: CurrentUser = Depends(require_authenticated_user)) -> RequestIdentity:
    """Dependency for public routes: the authenticated principal as a ``RequestIdentity``.

    Authentication is ``require_authenticated_user`` (API-11); identity comes only from
    gateway headers (API-1).
    """
    extra = get_extra_identity()
    return RequestIdentity(
        tenant_id=_parse_uuid(user.tenant_id),
        principal_id=_parse_uuid(user.user_id),
        roles=frozenset(user.roles),
        departments=extra.departments,
        plan=extra.plan,
        feature_flags=extra.feature_flags,
        residency_mode=extra.residency_mode,
    )
