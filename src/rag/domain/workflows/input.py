"""``GenerationWorkflowInput`` (LLD §9.2): identifiers and a scope seed, never content.

ASYNC-12 / GEN-11: nothing generated, retrieved or prompted rides here. ASYNC-6: the scope is a
*seed*, never a resolved token. ASYNC-11: no departments and no roles, both re-read per section.
The value object refuses a field it does not know and any value that is not a scalar identifier,
so a later edit cannot smuggle content in by adding a key.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

from rag.domain.generation.exceptions import InvariantViolation

FORBIDDEN_KEYS = frozenset({"departments", "roles", "scope_token", "token", "content", "prompt"})
"""Names that would mean authority or content in a run input (ASYNC-6, ASYNC-11, ASYNC-12)."""
MAX_SCALAR_CHARS = 512
"""No identifier or reference is longer; a longer value is content by another name."""


@dataclass(frozen=True, slots=True)
class ScopeSeed:
    """What a section's re-resolution starts from: the tender and the caller's named narrowing.

    Never an authority (SCOPE-3): every section re-resolves it against current grants.
    """

    tender_id: UUID | None = None
    collection_ids: tuple[UUID, ...] = ()
    collection_types: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        """JSON form."""
        return {
            "tender_id": None if self.tender_id is None else str(self.tender_id),
            "collection_ids": [str(c) for c in self.collection_ids],
            "collection_types": list(self.collection_types),
        }

    @staticmethod
    def from_dict(raw: Mapping[str, Any]) -> ScopeSeed:
        """Inverse of ``to_dict``."""
        tender = raw.get("tender_id")
        return ScopeSeed(
            tender_id=None if tender is None else UUID(str(tender)),
            collection_ids=tuple(UUID(str(c)) for c in raw.get("collection_ids", [])),
            collection_types=tuple(str(t) for t in raw.get("collection_types", [])),
        )


@dataclass(frozen=True, slots=True)
class WorkflowInput:
    """The start argument and the stored ``generation_runs.run_input`` (RUN-8)."""

    tenant_id: UUID
    principal_id: UUID
    artefact_id: UUID
    generation_run_id: UUID
    capability: str
    requested_mode: str
    plan: str | None
    residency_mode: str
    domain_profile_version: str
    language_pack_version: str
    scope_seed: ScopeSeed
    tender_id: UUID | None = None
    target_ref: str | None = None
    trace_id: str = ""
    correlation_id: str = ""
    kind: str = "document"
    """``document`` or ``extraction`` (W-6): which workflow shape the Worker Pool runs."""
    outline_gate: bool = True
    origin: str = "original"
    base_version_id: UUID | None = None
    instruction: str | None = None
    """A-5's reviewer instruction: the reviewer's own words, not generated or retrieved text."""
    extras: dict[str, str] = field(default_factory=dict)
    """Further identifiers (``schema_ref`` of W-6). Scalars only."""
    recipients: tuple[tuple[str, str], ...] = ()
    """``(kind, id)`` echoed on every event of the run (LD-83); identifiers only."""

    def __post_init__(self) -> None:
        for key, value in self.extras.items():
            if key in FORBIDDEN_KEYS:
                raise InvariantViolation("ASYNC-12", f"{key} may not ride in a run input")
            if not isinstance(value, str) or len(value) > MAX_SCALAR_CHARS:
                raise InvariantViolation("ASYNC-12", f"{key} is not a short identifier")
        for name in ("capability", "target_ref", "correlation_id", "trace_id"):
            value = getattr(self, name)
            if value is not None and len(value) > MAX_SCALAR_CHARS:
                raise InvariantViolation("ASYNC-12", f"{name} is not a short identifier")
        if self.origin != "original" and self.base_version_id is None:
            raise InvariantViolation("RUN-1", "a regeneration input names its base version")

    def to_dict(self) -> dict[str, Any]:
        """The JSON object stored and sent."""
        out: dict[str, Any] = {
            "tenant_id": str(self.tenant_id),
            "tender_id": None if self.tender_id is None else str(self.tender_id),
            "principal_id": str(self.principal_id),
            "trace_id": self.trace_id,
            "correlation_id": self.correlation_id,
            "artefact_id": str(self.artefact_id),
            "generation_run_id": str(self.generation_run_id),
            "capability": self.capability,
            "target_ref": self.target_ref,
            "requested_mode": self.requested_mode,
            "plan": self.plan,
            "residency_mode": self.residency_mode,
            "domain_profile_version": self.domain_profile_version,
            "language_pack_version": self.language_pack_version,
            "scope_seed": self.scope_seed.to_dict(),
            "kind": self.kind,
            "outline_gate": self.outline_gate,
            "origin": self.origin,
        }
        if self.base_version_id is not None:
            out["base_version_id"] = str(self.base_version_id)
        if self.instruction is not None:
            out["instruction"] = self.instruction
        if self.recipients:
            out["recipients"] = [{"kind": k, "id": i} for k, i in self.recipients]
        out.update(self.extras)
        return out
