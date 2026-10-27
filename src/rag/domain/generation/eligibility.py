"""Generation eligibility and the clamp (LLD §7.6, GEN-6, MD-23, AUTH-2).

Resolved before retrieval, clamps down only, and is written to the run at admission.
"""

from __future__ import annotations

from dataclasses import dataclass

from rag.domain.packs.generation import EligibilityDecl
from rag.domain.shared.enums import EligibilityState, GenerationMode
from rag.domain.shared.errors import DomainError, ErrorCode

CLAMP_REASON_PARTIAL = "profile_withholds_unattended_completion"
"""Used where the profile declares ``PARTIALLY_GENERATABLE`` and gives no reason of its own."""


@dataclass(frozen=True, slots=True)
class EligibilityVerdict:
    """§7.6's ``EligibilityVerdict``, carried on every generative response (API-5)."""

    state: EligibilityState
    requested_mode: GenerationMode
    effective_mode: GenerationMode
    clamped: bool
    profile_version: str
    clamp_reason: str | None = None


def resolve_eligibility(
    decl: EligibilityDecl, requested: GenerationMode, profile_version: str
) -> EligibilityVerdict:
    """The verdict, or ``403 GENERATION_NOT_PERMITTED`` carrying the declared reason.

    ``PARTIALLY_GENERATABLE`` allows assistance and withholds unattended completion: an
    automation request succeeds as assistance with the clamp recorded, never as a degraded
    automation mode (MD-23). The clamp only ever lowers the mode.
    """
    if decl.state is EligibilityState.NOT_GENERATABLE:
        raise DomainError(
            ErrorCode.GENERATION_NOT_PERMITTED,
            decl.reason or "The Domain Profile does not permit generation for this scope.",
        )
    clamped = (
        decl.state is EligibilityState.PARTIALLY_GENERATABLE
        and requested is GenerationMode.AUTOMATION
    )
    return EligibilityVerdict(
        state=decl.state,
        requested_mode=requested,
        effective_mode=GenerationMode.ASSISTANCE if clamped else requested,
        clamped=clamped,
        clamp_reason=(decl.reason or CLAMP_REASON_PARTIAL) if clamped else None,
        profile_version=profile_version,
    )
