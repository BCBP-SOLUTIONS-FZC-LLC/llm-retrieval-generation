"""The Worker Pool boundary types (workers LLD §4.3). Standard library only.

A Temporal workflow runs in a sandbox that rejects imports which pull in database pools or an S3
client, so the workflow code imports this module and nothing from ``rag``. Every type here is
JSON-serialisable and holds identifiers, enums, numbers and timestamps. **None holds generated
text, chunk text, a prompt or a document** (ASYNC-12, WP-9); the reviewer's instruction of an A-5
run travels as ``instruction_ref`` and is read back through the engine.

``GenerationWorkflowInput.from_dict`` is the one decoder of the start argument. It accepts both
shapes the engine meets: the stored ``generation_runs.run_input`` (a restart reads that, RUN-8) and
what the API's Temporal client sends (``instruction`` replaced by ``instruction_ref``). A workflow
therefore takes the start argument as a ``dict`` and decodes it here, so a new optional key in a
future API version cannot fail a run mid-flight.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

WORKFLOW_GENERATION = "GenerationWorkflow"
WORKFLOW_EXTRACTION = "ExtractionWorkflow"
WORKFLOW_SECTION = "SectionWorkflow"
SIGNAL_APPROVE = "approve_outline"
SIGNAL_REVISE = "revise_outline"
SIGNAL_CANCEL = "cancel"
SIGNAL_RESUME = "resume"

_REQUIRED = (
    "tenant_id",
    "principal_id",
    "artefact_id",
    "generation_run_id",
    "capability",
    "requested_mode",
    "residency_mode",
    "domain_profile_version",
    "language_pack_version",
)
_KNOWN = frozenset(
    {
        *_REQUIRED,
        "tender_id",
        "trace_id",
        "correlation_id",
        "target_ref",
        "plan",
        "scope_seed",
        "kind",
        "outline_gate",
        "origin",
        "base_version_id",
        "instruction",
        "instruction_ref",
        "recipients",
    }
)


@dataclass(frozen=True, slots=True)
class Recipient:
    """``(kind, id)`` echoed on every event of the run (LD-83)."""

    kind: str
    id: str


@dataclass(frozen=True, slots=True)
class ScopeSeed:
    """Where a section's re-resolution starts. Never an authority (SCOPE-3, ASYNC-6)."""

    tender_id: str | None = None
    collection_ids: tuple[str, ...] = ()
    collection_types: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class GenerationWorkflowInput:
    """The start argument of ``GenerationWorkflow`` and ``ExtractionWorkflow``.

    Identifiers and a scope seed only. There are no departments and no roles: both are re-read from
    IAM per section (ASYNC-11).
    """

    tenant_id: str
    principal_id: str
    artefact_id: str
    generation_run_id: str
    capability: str
    requested_mode: str
    residency_mode: str
    domain_profile_version: str
    language_pack_version: str
    tender_id: str | None = None
    trace_id: str = ""
    correlation_id: str = ""
    target_ref: str | None = None
    plan: str | None = None
    scope_seed: ScopeSeed = field(default_factory=ScopeSeed)
    kind: str = "document"
    """``document`` or ``extraction``."""
    outline_gate: bool = True
    """Whether the run waits for an outline approval. The API decided it; the workflow never
    re-derives it from the pack (a caller may ask for a gate on a pack that has none)."""
    origin: str = "original"
    base_version_id: str | None = None
    instruction_ref: str | None = None
    """A-5 only: the run id whose stored input holds the reviewer's words."""
    recipients: tuple[Recipient, ...] = ()
    extras: Mapping[str, str] = field(default_factory=dict)
    """Further identifiers: ``schema_ref`` and ``trigger_document_version_id`` of W-6."""

    @staticmethod
    def from_dict(raw: Mapping[str, Any]) -> GenerationWorkflowInput:
        """Decode the stored or the wire form. A missing required key raises ``KeyError``."""
        missing = [k for k in _REQUIRED if k not in raw]
        if missing:
            raise KeyError(f"run input lacks {', '.join(missing)}")
        seed = raw.get("scope_seed") or {}
        # the stored form still carries the words; the reference is the run's own id (ASYNC-12)
        ref = raw.get("instruction_ref")
        if ref is None and raw.get("instruction") is not None:
            ref = str(raw["generation_run_id"])
        return GenerationWorkflowInput(
            tenant_id=str(raw["tenant_id"]),
            principal_id=str(raw["principal_id"]),
            artefact_id=str(raw["artefact_id"]),
            generation_run_id=str(raw["generation_run_id"]),
            capability=str(raw["capability"]),
            requested_mode=str(raw["requested_mode"]),
            residency_mode=str(raw["residency_mode"]),
            domain_profile_version=str(raw["domain_profile_version"]),
            language_pack_version=str(raw["language_pack_version"]),
            tender_id=None if raw.get("tender_id") is None else str(raw["tender_id"]),
            trace_id=str(raw.get("trace_id") or ""),
            correlation_id=str(raw.get("correlation_id") or ""),
            target_ref=None if raw.get("target_ref") is None else str(raw["target_ref"]),
            plan=None if raw.get("plan") is None else str(raw["plan"]),
            scope_seed=ScopeSeed(
                None if seed.get("tender_id") is None else str(seed["tender_id"]),
                tuple(str(c) for c in seed.get("collection_ids", ())),
                tuple(str(t) for t in seed.get("collection_types", ())),
            ),
            kind=str(raw.get("kind") or "document"),
            outline_gate=bool(raw.get("outline_gate", True)),
            origin=str(raw.get("origin") or "original"),
            base_version_id=None
            if raw.get("base_version_id") is None
            else str(raw["base_version_id"]),
            instruction_ref=None if ref is None else str(ref),
            recipients=tuple(
                Recipient(str(r["kind"]), str(r["id"])) for r in raw.get("recipients", ())
            ),
            extras={str(k): str(v) for k, v in raw.items() if k not in _KNOWN and v is not None},
        )

    @property
    def is_regeneration(self) -> bool:
        """An A-5 run."""
        return self.origin != "original"


@dataclass(frozen=True, slots=True)
class SectionRef:
    """One section as the workflow plans with it."""

    section_id: str
    ref: str | None
    section_type: str
    is_foundation: bool
    state: str
    ordinal: int


@dataclass(frozen=True, slots=True)
class RunState:
    """The run as ``run_state`` reads it: what a restarted workflow resumes from."""

    status: str
    stage: str | None
    outline_state: str
    outline_version: int | None
    gate_expires_at: str | None
    suspended_at: str | None
    failure_reason: str | None
    section_refs: tuple[SectionRef, ...] = ()


@dataclass(frozen=True, slots=True)
class OutlineSaved:
    """What ``save_outline`` returns. ``gate_expires_at`` is ``None`` where no gate applies."""

    section_ids: tuple[str, ...]
    outline_version: int | None
    gate_expires_at: str | None


@dataclass(frozen=True, slots=True)
class SectionJob:
    """The input of one ``SectionWorkflow``: a section, its stage and the run's wire input."""

    section_id: str
    stage: str
    run_input: Mapping[str, Any]


@dataclass(frozen=True, slots=True)
class SectionOutcome:
    """How one section ended. A quota refusal is a result, never an exception (WP-8)."""

    section_id: str
    state: str
    """``complete``, ``gap`` or ``failed``."""
    version_id: str | None = None
    gap_reason: str | None = None
    failure_reason: str | None = None
    evidence_tier: str | None = None
    quota_suspended: bool = False
    skipped_terminal: bool = False


@dataclass(frozen=True, slots=True)
class MintedVersion:
    """The document version ``mint_document_version`` wrote, for ``complete_run``."""

    version_id: str
    version_no: int
    evidence_tier: str
    completeness: str


@dataclass(frozen=True, slots=True)
class ExtractionResult:
    """How ``run_extraction`` ended.

    ``refused`` means the evidence was insufficient (R-4's 200 with every field a gap): the run is
    closed and the workflow ends without writing it.
    """

    state: str
    """``complete`` or ``refused``."""
    artefact_version_id: str | None = None
    evidence_tier: str | None = None
    refusal_reason: str | None = None


@dataclass(frozen=True, slots=True)
class CoherenceResult:
    """The cross-section check."""

    verdict: str
    conflict_count: int


@dataclass(frozen=True, slots=True)
class RunResult:
    """What the workflow returns. Callers read progress from the tables, never from this."""

    status: str
    failure_reason: str | None = None


@dataclass(frozen=True, slots=True)
class SectionSpec:
    """One edited outline entry of a revision: a reference and a type, never content."""

    ref: str
    type: str


@dataclass(frozen=True, slots=True)
class ApproveOutlinePayload:
    """``approve_outline`` (W-3)."""

    outline_version: int


@dataclass(frozen=True, slots=True)
class ReviseOutlinePayload:
    """``revise_outline`` (W-4): the version is already incremented by the API."""

    outline_version: int
    sections: tuple[SectionSpec, ...] = ()


@dataclass(frozen=True, slots=True)
class CancelPayload:
    """``cancel`` (W-5, a purge, or a ``scope_revoked`` end)."""

    reason: str = ""
