"""LLD §24: the taxonomy table and its mapping to fastapicommon problem types."""

from __future__ import annotations

import pytest

from rag.domain.shared.errors import ERROR_TAXONOMY, PROBLEM_DOMAIN, DomainError, ErrorCode
from rag.presentation.errors import PROBLEM_TYPES

STATUS_BY_CODE = {
    "COLLECTION_NOT_FOUND": 404,
    "COLLECTION_FROZEN": 409,
    "COLLECTION_PURGING": 410,
    "USER_INPUT_OVER_BUDGET": 413,
    "REQUIRED_INPUT_MISSING": 422,
    "QUOTA_EXHAUSTED": 429,
    "SCOPE_STORE_UNAVAILABLE": 503,
    "OPERATOR_REQUIRED": 403,
}


def test_taxonomy_has_the_32_live_domain_codes() -> None:
    """§24: 32 live domain codes; the five platform codes are not among them."""
    assert len(ErrorCode) == len(ERROR_TAXONOMY) == 32
    assert not {
        "VALIDATION_FAILED",
        "UNAUTHORIZED",
        "INTERNAL_ERROR",
        "REQUEST_TIMEOUT",
        "FORBIDDEN",
    } & {c.value for c in ErrorCode}


@pytest.mark.parametrize(("code", "status"), STATUS_BY_CODE.items())
def test_status_per_taxonomy_row(code: str, status: int) -> None:
    """Spot-check statuses from §24, including the lone 410."""
    assert ERROR_TAXONOMY[ErrorCode(code)].status == status


def test_every_code_maps_to_a_problem_type() -> None:
    """Each row becomes a problem type in the retrieval-generation domain with the same code."""
    assert set(PROBLEM_TYPES) == set(ErrorCode)
    for code, spec in ERROR_TAXONOMY.items():
        pt = PROBLEM_TYPES[code]
        assert (pt.domain, pt.name, pt.title, pt.status, pt.code) == (
            PROBLEM_DOMAIN,
            code.value.lower().replace("_", "-"),
            spec.title,
            spec.status,
            code.value,
        )


def test_domain_error_carries_its_spec() -> None:
    """DomainError exposes the code and spec; the message falls back to the title."""
    err = DomainError(ErrorCode.COLLECTION_NOT_FOUND)
    assert (err.code, err.spec.status, str(err)) == (
        ErrorCode.COLLECTION_NOT_FOUND,
        404,
        "Collection not found",
    )
