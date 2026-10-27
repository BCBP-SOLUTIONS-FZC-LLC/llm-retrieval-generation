"""The request principal: who is calling, with what authority and plan."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from uuid import UUID

READ_ONLY_FLAG = "read_only"
"""``x-feature-flags`` member IAM sets for a cancelled tenant's grace period (LD-46)."""


class ResidencyMode(StrEnum):
    """Tenant data-residency mode; an unknown header value means strict (IAM HLD, LD-74)."""

    STANDARD = "standard"
    STRICT = "strict"


@dataclass(frozen=True, slots=True)
class DepartmentRole:
    """One ``<department-uuid>:<role-level>`` pair of ``x-departments`` (§22.10)."""

    department_id: UUID
    role: str


@dataclass(frozen=True, slots=True)
class RequestIdentity:
    """Gateway-asserted identity of one request (API-1: never taken from a body).

    ``principal_id`` is ``None`` on internal routes, which carry only a tenant (§5.1).
    """

    tenant_id: UUID
    principal_id: UUID | None = None
    roles: frozenset[str] = frozenset()
    departments: tuple[DepartmentRole, ...] = ()
    plan: str | None = None
    feature_flags: frozenset[str] = field(default_factory=frozenset)
    residency_mode: ResidencyMode = ResidencyMode.STANDARD

    def has_role(self, role: str) -> bool:
        """Whether the caller holds ``role`` in the tenant."""
        return role in self.roles

    @property
    def read_only(self) -> bool:
        """Whether the tenant is in IAM's read-only grace period (generation is refused)."""
        return READ_ONLY_FLAG in self.feature_flags
