"""Domain Profile and Language Pack value objects (LLD §22.9), as retrieval consumes them.

Only the fields a retrieval or classification path reads are modelled. Eligibility, prompt
skeleton text, extraction schemas and the like belong to the generation slice and are added
there. Both packs are resolved per tenant at one pinned version (``ASYNC-3``).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Protocol
from uuid import UUID

from rag.domain.packs.generation import (
    ContentRule,
    EligibilityDecl,
    ExtractionSchema,
)
from rag.domain.retrieval.sufficiency import RequiredInputRule, SufficiencyThreshold


class ProfileSource(StrEnum):
    """Which profile row a request ran under (§6.7 ``profile_source``)."""

    SECTION_TYPE = "section_type"
    PROFILE_DEFAULT = "profile_default"
    PLATFORM_DEFAULT = "platform_default"


class PackSource(StrEnum):
    """Where the bundle came from."""

    TENDER = "tender"
    PLATFORM_DEFAULT = "platform_default"


@dataclass(frozen=True, slots=True)
class SectionTypeProfile:
    """Retrieval parameters of one section type (HLD §7.5, LLD §6.7, §6.8)."""

    section_type: str
    candidate_depth: int
    rerank_in: int
    rerank_out: int
    sufficiency: SufficiencyThreshold
    diversity_bound: int
    values_provisional: bool = True
    vocabulary: tuple[str, ...] = ()
    mandatory_clauses: frozenset[str] = frozenset()
    preferred_roles: tuple[str, ...] = ()
    skeleton: str = ""
    """Prompt layer 1 text for this section type (§7.2): instruction authority."""
    compliance: bool = False
    """Compliance-typed (HLD §8.5): phase 1 suppressed, tier 0 mandated, absolute gate (GR-4)."""
    failure_policy: str | None = None
    """``block`` or ``flag``; ``None`` means ``block`` for compliance and ``flag`` otherwise."""
    disclaimer_ref: str | None = None

    @property
    def policy(self) -> str:
        """The effective failure policy (§8.1): compliance blocks, the rest flag."""
        return self.failure_policy or ("block" if self.compliance else "flag")


@dataclass(frozen=True, slots=True)
class DomainProfile:
    """A resolved Domain Profile."""

    ref: str
    version: str
    taxonomy_version: str
    default_profile: SectionTypeProfile
    section_profiles: Mapping[str, SectionTypeProfile] = field(default_factory=dict)
    required_inputs: Mapping[str, RequiredInputRule] = field(default_factory=dict)
    document_roles: tuple[str, ...] = ()
    entity_keys: tuple[str, ...] = ()
    skeleton_tokens: int = 0
    answer_skeleton: str = ""
    """Layer 1 for R-2, which carries a question and no section type."""
    tenant_overrides: str = ""
    """Layer 2: tenant-authored boilerplate. Content, never instruction (GEN-4)."""
    eligibility: Mapping[str, EligibilityDecl] = field(default_factory=dict)
    default_eligibility: EligibilityDecl = field(default_factory=EligibilityDecl)
    """Platform default is GENERATABLE: absence of a declaration never withholds generation."""
    generation_model_profile: str | None = None
    judge_model_profile: str | None = None
    content_rules: tuple[ContentRule, ...] = ()
    extraction_schemas: Mapping[str, ExtractionSchema] = field(default_factory=dict)
    outline_gate_required: bool = False
    """The pack forbids skipping the human gate (W-1 ``outline_gate: false`` is then overridden,
    §5.7.12 "where the pack permits it"). Assumed pack key; the LLD names no field for it."""

    def profile_for(self, section_type: str | None) -> tuple[SectionTypeProfile, ProfileSource]:
        """The section-type row, else the profile default (§6.7 ladder)."""
        if section_type is not None and section_type in self.section_profiles:
            return self.section_profiles[section_type], ProfileSource.SECTION_TYPE
        return self.default_profile, ProfileSource.PROFILE_DEFAULT

    def eligibility_for(self, capability: str) -> EligibilityDecl:
        """The declaration for ``capability``: ``answer``, the section type, or ``extract``."""
        return self.eligibility.get(capability, self.default_eligibility)

    def rule_for(self, section_type: str) -> RequiredInputRule:
        """The required-input rule of a section type; none declared means nothing is required."""
        return self.required_inputs.get(section_type, RequiredInputRule())


@dataclass(frozen=True, slots=True)
class LanguagePack:
    """A resolved Language Pack: the BCP 47 tag, its version and its terminology."""

    language: str
    version: str
    terminology: Mapping[str, tuple[str, ...]] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class PackBundle:
    """The Domain Profile and Language Pack a request pins."""

    domain_profile: DomainProfile
    language_pack: LanguagePack
    source: PackSource = PackSource.TENDER

    @property
    def is_platform_default(self) -> bool:
        """Retrieval ran on the fallback profile, which must carry a marker (LD-14)."""
        return self.source is PackSource.PLATFORM_DEFAULT


class PackUnavailable(Exception):  # noqa: N818
    """Tender cannot supply a pack: breaker open, or failure after the retries (§14.3)."""


class PackCache(Protocol):
    """Version-keyed pack cache (§11.1 ``{tenant}:pack:{identity}:{version}``, 1 h)."""

    async def get(self, tenant_id: UUID, identity: str, version: str | None) -> PackBundle | None:
        """The cached bundle, or ``None`` (a failing cache is a miss)."""
        ...

    async def put(
        self, tenant_id: UUID, identity: str, version: str | None, bundle: PackBundle
    ) -> None:
        """Store ``bundle``; also under its own version so a pinned read hits."""
        ...


DEFAULT_SUFFICIENCY = SufficiencyThreshold(min_top_score=0.30, min_chunks=2, chunk_score_floor=0.10)
DEFAULT_SECTION = SectionTypeProfile(
    section_type="*",
    candidate_depth=24,
    rerank_in=20,
    rerank_out=8,
    sufficiency=DEFAULT_SUFFICIENCY,
    diversity_bound=3,
    values_provisional=True,
)
PLATFORM_DEFAULT_PROFILE = DomainProfile(
    ref="platform-default",
    version="platform-default-1",
    taxonomy_version="platform-default-1",
    default_profile=DEFAULT_SECTION,
    document_roles=(
        "specification",
        "drawing",
        "bill_of_quantities",
        "general_conditions",
        "special_conditions",
        "other",
        "unknown",
    ),
)
PLATFORM_DEFAULT_BUNDLE = PackBundle(
    PLATFORM_DEFAULT_PROFILE,
    LanguagePack(language="und", version="platform-default-1"),
    PackSource.PLATFORM_DEFAULT,
)
"""HLD §7.5's "no Domain Profile resolved -> platform default". Every value is provisional
(RET-4); the numbers are design defaults (CFG-1), not measurements."""
