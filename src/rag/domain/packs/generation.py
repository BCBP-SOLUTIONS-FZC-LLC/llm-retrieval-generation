"""Pack data the generation slice reads (LLD §7.2, §7.6, §7.7, §15.3): value objects only.

Eligibility declarations, the input-screening policy and extraction schemas. All of it is pack
data because it changes without a deploy and differs per domain (MD-04, MD-23).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field

from rag.domain.shared.enums import EligibilityState


@dataclass(frozen=True, slots=True)
class EligibilityDecl:
    """A capability's eligibility state, with the profile-declared reason where not GENERATABLE."""

    state: EligibilityState = EligibilityState.GENERATABLE
    reason: str | None = None


@dataclass(frozen=True, slots=True)
class ContentRule:
    """One input-screening rule (§15.3): a regular expression over layers 2 to 5.

    ``layers`` limits the rule to named layers; empty means every screened layer.
    """

    name: str
    pattern: str
    layers: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class FieldSpec:
    """One field of an extraction schema (§7.7): type and post-generation constraints."""

    name: str
    type: str = "string"  # string | number | integer | boolean | date | enum
    description: str = ""
    pattern: str | None = None
    minimum: float | None = None
    maximum: float | None = None
    choices: tuple[str, ...] = ()
    conditional: bool = False
    """The profile marks the field conditional: ``not_applicable`` is a legal gap reason."""


@dataclass(frozen=True, slots=True)
class ExtractionSchema:
    """The resolved schema for one ``schema_ref`` at one version (§7.7)."""

    ref: str
    version: str
    fields: tuple[FieldSpec, ...] = field(default_factory=tuple)
    description: str = ""

    @property
    def versioned_ref(self) -> str:
        """``output_schema_version``: ``<ref>@<version>`` (§5.7.4)."""
        return f"{self.ref}@{self.version}"


EligibilityMap = Mapping[str, EligibilityDecl]
