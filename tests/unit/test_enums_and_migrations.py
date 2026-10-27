"""Enum orderings, and parity between Python enums and the 0001 migrations' CREATE TYPEs."""

from __future__ import annotations

import re
from enum import StrEnum
from pathlib import Path

import pytest

from rag.domain.shared import enums
from rag.domain.shared.enums import EvidenceTier, GrantLevel

MIGRATIONS = Path(__file__).resolve().parents[2] / "migrations"
CORPUS_ONLY = {
    "collection_type",
    "index_generation_status",
    "collection_status",
    "grant_subject_type",
}


def _snake(name: str) -> str:
    return re.sub(r"(?<!^)(?=[A-Z])", "_", name).lower()


PY_ENUMS = {
    _snake(c.__name__): [m.value for m in c]
    for c in vars(enums).values()
    if isinstance(c, type)
    and issubclass(c, StrEnum)
    and c.__module__ == enums.__name__
    and c is not enums.OrderedStrEnum
}


def _sql_enums(instance: str) -> dict[str, list[str]]:
    sql = (MIGRATIONS / instance / "0001_types_and_functions.up.sql").read_text()
    return {
        name: re.findall(r"'([^']*)'", labels)
        for name, labels in re.findall(r"CREATE TYPE (\w+) AS ENUM \(([^)]*)\)", sql)
    }


def test_evidence_tier_rollup_is_min() -> None:
    """NOT_COMPUTED sorts lowest, so min() is the lowest-wins roll-up (MD-30)."""
    tiers = [EvidenceTier.SUPPORTED, EvidenceTier.NOT_COMPUTED, EvidenceTier.PARTIAL]
    assert min(tiers) is EvidenceTier.NOT_COMPUTED
    assert (
        EvidenceTier.NOT_COMPUTED
        < EvidenceTier.INSUFFICIENT
        < EvidenceTier.PARTIAL
        < EvidenceTier.SUPPORTED
    )


def test_grant_level_clamps_down() -> None:
    """search < answer < generate; a clamp is min() (SCOPE-8)."""
    assert GrantLevel.SEARCH < GrantLevel.ANSWER < GrantLevel.GENERATE
    assert min(GrantLevel.GENERATE, GrantLevel.ANSWER) is GrantLevel.ANSWER
    assert GrantLevel.GENERATE >= GrantLevel.ANSWER


def test_ordered_enums_do_not_compare_across_types() -> None:
    """Comparing two different enums is a TypeError, not a silent string comparison."""
    with pytest.raises(TypeError):
        _ = GrantLevel.SEARCH < EvidenceTier.SUPPORTED  # type: ignore[operator]


def test_python_enums_cover_all_27_types() -> None:
    """LLD §4: 27 enumerated types (23 generation, 4 corpus-only; grant_level is in both)."""
    assert len(PY_ENUMS) == 27


@pytest.mark.parametrize("instance", ["corpus", "generation"])
def test_migration_enums_match_python(instance: str) -> None:
    """Every CREATE TYPE in an instance's 0001 equals the Python enum, labels and order."""
    sql = _sql_enums(instance)
    if instance == "corpus":
        expected = {n: v for n, v in PY_ENUMS.items() if n in CORPUS_ONLY | {"grant_level"}}
    else:
        expected = {n: v for n, v in PY_ENUMS.items() if n not in CORPUS_ONLY}
    assert sql == expected


@pytest.mark.parametrize("instance", ["corpus", "generation"])
def test_every_up_has_a_down(instance: str) -> None:
    """Each .up.sql has its .down.sql, and 0000/0001 are present first."""
    ups = sorted((MIGRATIONS / instance).glob("*.up.sql"))
    assert [p.name[:4] for p in ups[:2]] == ["0000", "0001"]
    assert all(p.with_name(p.name.replace(".up.", ".down.")).exists() for p in ups)
