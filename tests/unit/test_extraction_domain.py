"""Extraction output validation, gaps and provenance (§7.7, GEN-8, LD-17)."""

from __future__ import annotations

import json
from uuid import UUID

import pytest

from rag.domain.generation.extraction import all_gaps, fields_to_json, parse_extraction
from rag.domain.generation.prompt import PromptChunk
from rag.domain.packs.generation import ExtractionSchema, FieldSpec
from rag.domain.shared.errors import DomainError, ErrorCode

DOC = UUID("0a3f0000-0000-7000-8000-0000000000d1")
SCHEMA = ExtractionSchema(
    "dp/summary",
    "2.1.0",
    (
        FieldSpec("contract_value", "number", minimum=0),
        FieldSpec("completion_date", "date"),
        FieldSpec("region", "enum", choices=("EU", "UK")),
        FieldSpec("reference", "string", pattern=r"TN-\d{4}"),
        FieldSpec("lots", "integer"),
    ),
)
CHUNKS = [
    PromptChunk("v1:1", "Value EUR 4,820,000", "Doc", "2 > 2.1", "2.1", document_id=DOC,
                document_version=3, language="de", excerpt="Value EUR 4,820,000"),
]  # fmt: skip
REFS = {"c-01": "v1:1"}


def out(**fields: object) -> str:
    return json.dumps({"fields": fields})


def test_supported_value_carries_provenance_and_missing_fields_are_gaps() -> None:
    r = parse_extraction(
        SCHEMA, out(contract_value={"value": 4820000, "chunk_ids": ["c-01"]}), CHUNKS, REFS
    )
    j = fields_to_json(r)
    assert list(j) == [f.name for f in SCHEMA.fields]  # schema order, every field present
    p = j["contract_value"]["provenance"][0]
    assert p["chunk_id"] == "v1:1" and p["document_id"] == str(DOC) and p["document_version"] == 3
    assert p["clause_ref"] == "2.1" and p["path"] == "2 > 2.1" and p["source_span"] is None
    assert p["excerpt_language"] == "en" and p["excerpt_is_translation"] is True  # German source
    assert j["completion_date"] == {
        "value": None,
        "gap": True,
        "gap_reason": "no_supporting_evidence",
    }


def test_a_value_with_no_supporting_chunk_is_a_gap_not_a_bare_value() -> None:
    """GEN-8: provenance or gap, no third state."""
    r = parse_extraction(SCHEMA, out(contract_value={"value": 5, "chunk_ids": []}), CHUNKS, REFS)
    assert fields_to_json(r)["contract_value"]["gap"] is True
    r = parse_extraction(
        SCHEMA, out(contract_value={"value": 5, "chunk_ids": ["c-77"]}), CHUNKS, REFS
    )
    assert fields_to_json(r)["contract_value"]["gap"] is True


@pytest.mark.parametrize(
    "fields",
    [
        {"contract_value": {"value": -1, "chunk_ids": ["c-01"]}},  # range
        {"contract_value": {"value": "ten", "chunk_ids": ["c-01"]}},  # type
        {"completion_date": {"value": "next week", "chunk_ids": ["c-01"]}},  # format
        {"region": {"value": "US", "chunk_ids": ["c-01"]}},  # enum
        {"reference": {"value": "XX-1", "chunk_ids": ["c-01"]}},  # pattern
        {"lots": {"value": 2.5, "chunk_ids": ["c-01"]}},  # integer
        {"surprise": {"value": 1, "chunk_ids": ["c-01"]}},  # not in the schema
    ],
)
def test_constraints_are_validated_after_generation(fields: dict[str, object]) -> None:
    with pytest.raises(DomainError) as exc:
        parse_extraction(SCHEMA, out(**fields), CHUNKS, REFS)
    assert exc.value.code is ErrorCode.EXTRACTION_SCHEMA_VIOLATION


def test_malformed_output_is_a_violation_that_never_echoes_the_value() -> None:
    for raw in ("not json", json.dumps([1]), json.dumps({"nope": {}})):
        with pytest.raises(DomainError) as exc:
            parse_extraction(SCHEMA, raw, CHUNKS, REFS)
        assert exc.value.code is ErrorCode.EXTRACTION_SCHEMA_VIOLATION
    with pytest.raises(DomainError) as exc:
        parse_extraction(SCHEMA, out(reference={"value": "SECRET-VALUE", "chunk_ids": ["c-01"]}),
                         CHUNKS, REFS)  # fmt: skip
    assert "SECRET-VALUE" not in (exc.value.detail or "")


def test_all_gaps_for_an_insufficient_scope() -> None:
    g = all_gaps(SCHEMA, "no_supporting_evidence")
    assert set(g) == {f.name for f in SCHEMA.fields} and all(v["gap"] for v in g.values())


# --- the rest of the constraint checks and the malformed shapes --------------------------------

LOOSE = ExtractionSchema(
    "dp/loose",
    "1.0.0",
    (
        FieldSpec("share", "number", maximum=100),
        FieldSpec("optional", "boolean"),
        FieldSpec("title", "string"),
    ),
)


def violation(schema: ExtractionSchema, raw: str) -> str:
    with pytest.raises(DomainError) as exc:
        parse_extraction(schema, raw, CHUNKS, REFS)
    assert exc.value.code is ErrorCode.EXTRACTION_SCHEMA_VIOLATION
    return exc.value.detail if hasattr(exc.value, "detail") else str(exc.value)


def test_a_number_above_its_maximum_is_refused_without_echoing_the_value() -> None:
    message = violation(LOOSE, out(share={"value": 250, "chunk_ids": ["c-01"]}))
    assert "share: above maximum" in message and "250" not in message


def test_a_boolean_field_takes_only_a_boolean() -> None:
    assert "optional: expected boolean" in violation(
        LOOSE, out(optional={"value": "yes", "chunk_ids": ["c-01"]})
    )


def test_a_string_field_takes_only_a_string_and_a_plain_string_has_no_constraint() -> None:
    assert "title: expected string" in violation(
        LOOSE, out(title={"value": 7, "chunk_ids": ["c-01"]})
    )
    ok = parse_extraction(LOOSE, out(title={"value": "Plan", "chunk_ids": ["c-01"]}), CHUNKS, REFS)
    assert [f.name for f in ok.fields if not f.gap] == ["title"]


def test_an_entry_that_is_not_an_object_is_refused() -> None:
    assert "title: expected an object" in violation(LOOSE, out(title="Plan"))


def test_output_whose_fields_is_not_a_map_is_refused() -> None:
    assert "not an object with a fields map" in violation(
        LOOSE, json.dumps({"fields": ["share", "title"]})
    )
