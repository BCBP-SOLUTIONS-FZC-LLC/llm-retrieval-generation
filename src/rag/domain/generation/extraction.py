"""Structured extraction output: validation, gaps and provenance (LLD §7.7, GEN-8, LD-17).

The schema is the resolved Domain Profile schema. Constraints (ranges, formats, patterns) are
validated after generation and never assumed of the model. Every field carries provenance or
is a gap: there is no third state.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date
from typing import Any
from uuid import UUID

from rag.domain.generation.prompt import PromptChunk
from rag.domain.packs.generation import ExtractionSchema, FieldSpec
from rag.domain.shared.errors import DomainError, ErrorCode

GAP_NO_EVIDENCE = "no_supporting_evidence"


@dataclass(frozen=True, slots=True)
class Provenance:
    """Field-level provenance (HLD §8.8, §5.7.4). ``source_span`` is null until D-33 closes."""

    document_id: UUID | None
    document_version: int
    chunk_id: str
    clause_ref: str | None
    page: int | None
    path: str | None
    excerpt: str
    excerpt_language: str = "en"
    excerpt_is_translation: bool = False
    source_span: dict[str, Any] | None = None


@dataclass(frozen=True, slots=True)
class ExtractedField:
    """One extracted value with its provenance, or a gap (GEN-8)."""

    name: str
    value: Any = None
    gap: bool = False
    gap_reason: str | None = None
    provenance: tuple[Provenance, ...] = ()


@dataclass(frozen=True, slots=True)
class ExtractionResult:
    """All schema fields, in schema order, gaps included."""

    schema: ExtractionSchema
    fields: tuple[ExtractedField, ...] = field(default_factory=tuple)


def _violation(problems: Sequence[str]) -> DomainError:
    return DomainError(
        ErrorCode.EXTRACTION_SCHEMA_VIOLATION,
        "The model output does not satisfy the resolved schema: " + "; ".join(problems[:10]),
    )


def _check(spec: FieldSpec, value: Any) -> str | None:
    """A problem sentence for ``value`` against ``spec``, or ``None``. Never echoes the value."""
    kind = spec.type
    if kind in {"number", "integer"}:
        if isinstance(value, bool) or not isinstance(value, int | float):
            return f"{spec.name}: expected {kind}"
        if kind == "integer" and float(value) != int(value):
            return f"{spec.name}: expected integer"
        if spec.minimum is not None and value < spec.minimum:
            return f"{spec.name}: below minimum"
        if spec.maximum is not None and value > spec.maximum:
            return f"{spec.name}: above maximum"
        return None
    if kind == "boolean":
        return None if isinstance(value, bool) else f"{spec.name}: expected boolean"
    if not isinstance(value, str):
        return f"{spec.name}: expected string"
    if kind == "date":
        try:
            date.fromisoformat(value)
        except ValueError:
            return f"{spec.name}: expected ISO date"
    if kind == "enum" and value not in spec.choices:
        return f"{spec.name}: not one of the allowed values"
    if spec.pattern is not None and re.fullmatch(spec.pattern, value) is None:
        return f"{spec.name}: does not match the pattern"
    return None


def parse_extraction(
    schema: ExtractionSchema,
    raw: str,
    chunks: Sequence[PromptChunk],
    refs: Mapping[str, str],
) -> ExtractionResult:
    """Validate ``raw`` model output against ``schema`` and attach provenance.

    ``refs`` maps the provisional ``c-NN`` markers to chunk ids. Raises
    ``422 EXTRACTION_SCHEMA_VIOLATION`` (non-retryable) on malformed JSON, an unknown field or a
    constraint failure. A null value, or a value no retrieved chunk supports, is a gap.
    """
    try:
        body = json.loads(raw)
        given = body["fields"]
        if not isinstance(given, dict):
            raise TypeError
    except (ValueError, KeyError, TypeError) as exc:
        raise _violation(["output is not an object with a fields map"]) from exc
    known = {f.name for f in schema.fields}
    problems = [f"{name}: not in the schema" for name in given if name not in known]
    by_id = {c.chunk_id: c for c in chunks}
    out: list[ExtractedField] = []
    for spec in schema.fields:
        entry = given.get(spec.name)
        if not isinstance(entry, dict) and entry is not None:
            problems.append(f"{spec.name}: expected an object")
            continue
        value = None if entry is None else entry.get("value")
        if value is None:
            out.append(ExtractedField(spec.name, gap=True, gap_reason=GAP_NO_EVIDENCE))
            continue
        if (problem := _check(spec, value)) is not None:
            problems.append(problem)
            continue
        cited = [
            by_id[refs.get(str(c), str(c))]
            for c in (entry or {}).get("chunk_ids", ())
            if refs.get(str(c), str(c)) in by_id
        ]
        if not cited:  # GEN-8: no supporting chunk means a gap, never a bare value
            out.append(ExtractedField(spec.name, gap=True, gap_reason=GAP_NO_EVIDENCE))
            continue
        out.append(
            ExtractedField(
                spec.name,
                value=value,
                provenance=tuple(
                    Provenance(
                        c.document_id,
                        c.document_version,
                        c.chunk_id,
                        c.clause_ref,
                        None,
                        c.heading_path,
                        c.excerpt,
                        excerpt_is_translation=not c.language.lower().startswith("en"),
                    )
                    for c in dict.fromkeys(cited)
                ),
            )
        )
    if problems:
        raise _violation(problems)
    return ExtractionResult(schema, tuple(out))


def fields_to_json(result: ExtractionResult) -> dict[str, dict[str, Any]]:
    """The §5.7.4 ``fields`` object: provenance on a value, ``gap: true`` on a gap (GEN-8)."""
    out: dict[str, dict[str, Any]] = {}
    for f in result.fields:
        if f.gap:
            out[f.name] = {"value": None, "gap": True, "gap_reason": f.gap_reason}
            continue
        out[f.name] = {
            "value": f.value,
            "provenance": [
                {
                    "document_id": None if p.document_id is None else str(p.document_id),
                    "document_version": p.document_version,
                    "chunk_id": p.chunk_id,
                    "clause_ref": p.clause_ref,
                    "page": p.page,
                    "path": p.path,
                    "excerpt": p.excerpt,
                    "excerpt_language": p.excerpt_language,
                    "excerpt_is_translation": p.excerpt_is_translation,
                    "source_span": p.source_span,
                }
                for p in f.provenance
            ],
        }
    return out


def all_gaps(schema: ExtractionSchema, reason: str) -> dict[str, dict[str, Any]]:
    """Every field a gap: the answer to an extraction whose evidence was insufficient."""
    return {f.name: {"value": None, "gap": True, "gap_reason": reason} for f in schema.fields}
