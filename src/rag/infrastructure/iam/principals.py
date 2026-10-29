"""IAM principal re-read for the quota-restored consumer (ASYNC-11, §22.5).

``GET /api/v1/internal/users/:id/memberships?tenant_id=`` (I-8, ``iam-lld-org-membership-service``):
the tenant is a query parameter, and the body carries ``roles`` and
``departments: [{department_id, code, role_level}]`` among other fields. A ``404`` is a principal
with no active membership in the tenant: an identity with no membership, which scope resolution
then finds empty (the run ends ``scope_revoked``). Any other failure refuses
(``SCOPE_STORE_UNAVAILABLE``) rather than guess: an empty department list built from an outage
would end live runs.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

import httpx
from fastapicommon.logging import get_logger
from fastapicommon.propagation import create_platform_http_client

from rag.domain.shared.errors import DomainError, ErrorCode
from rag.domain.shared.identity import DepartmentRole, RequestIdentity, ResidencyMode

log = get_logger("rag.principals")
TIMEOUT_SECONDS = 1.0


def _unavailable() -> DomainError:
    return DomainError(
        ErrorCode.SCOPE_STORE_UNAVAILABLE, "The principal's memberships cannot be read."
    )


class HttpPrincipalDirectory:
    """``PrincipalDirectory`` over IAM."""

    def __init__(self, base_url: str) -> None:
        self._base_url = base_url

    async def identity_for(
        self, tenant_id: UUID, principal_id: UUID, *, plan: str | None, residency_mode: str
    ) -> RequestIdentity:
        """The principal's current roles and departments, inside ``tenant_id``."""
        try:
            async with create_platform_http_client(
                base_url=self._base_url, timeout=TIMEOUT_SECONDS
            ) as client:
                response = await client.get(
                    f"/api/v1/internal/users/{principal_id}/memberships",
                    params={"tenant_id": str(tenant_id)},
                )
            if response.status_code == 404:
                body: dict[str, Any] = {}
            else:
                response.raise_for_status()
                body = response.json()
            if not isinstance(body, dict):
                raise ValueError("memberships is not an object")
            departments = tuple(
                DepartmentRole(UUID(str(d["department_id"])), str(d["role_level"]))
                for d in body.get("departments") or []
            )
            roles = frozenset(str(r) for r in body.get("roles") or [])
        except (httpx.HTTPError, ValueError, KeyError, TypeError) as exc:
            log.warning("memberships_unavailable", error=type(exc).__name__)
            raise _unavailable() from exc
        return RequestIdentity(
            tenant_id=tenant_id,
            principal_id=principal_id,
            roles=roles,
            departments=departments,
            plan=plan,
            residency_mode=(
                ResidencyMode.STANDARD if residency_mode == "standard" else ResidencyMode.STRICT
            ),
        )


class UnavailablePrincipalDirectory:
    """No IAM URL configured: nothing can be re-read, so nothing is resumed (fail closed)."""

    async def identity_for(
        self, tenant_id: UUID, principal_id: UUID, *, plan: str | None, residency_mode: str
    ) -> RequestIdentity:
        """Always refused."""
        raise _unavailable()
