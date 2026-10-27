"""Maps domain errors (``domain/shared/errors.py``) to ``fastapicommon`` RFC 9457 problems."""

from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapicommon.exceptions import problem_response
from fastapicommon.problem import TYPE_UNAUTHORIZED, ProblemType, problem_type
from pgcommon import IDENTITY_ERRORS

from rag.domain.shared.errors import ERROR_TAXONOMY, PROBLEM_DOMAIN, DomainError, ErrorCode

PROBLEM_TYPES: dict[ErrorCode, ProblemType] = {
    code: problem_type(PROBLEM_DOMAIN, spec.slug, spec.title, spec.status, code.value)
    for code, spec in ERROR_TAXONOMY.items()
}
"""One ``fastapicommon`` problem type per §24 domain code."""


async def domain_error_handler(request: Request, exc: Exception) -> JSONResponse:
    """Render a ``DomainError`` as its §24 problem, with ``Retry-After`` on a 429."""
    assert isinstance(exc, DomainError)
    response = problem_response(
        request, PROBLEM_TYPES[exc.code], detail=exc.detail, extensions=exc.extensions
    )
    if exc.retry_after_seconds is not None:
        response.headers["Retry-After"] = str(exc.retry_after_seconds)
    return response


async def identity_error_handler(request: Request, exc: Exception) -> JSONResponse:
    """An inconsistent GUC identity (user without tenant, bad role token) is a 401."""
    return problem_response(request, TYPE_UNAUTHORIZED, detail="The request identity is invalid.")


def register_error_handlers(app: FastAPI) -> None:
    """Install the domain and pgcommon-identity handlers on ``app``."""
    app.add_exception_handler(DomainError, domain_error_handler)
    for error_type in IDENTITY_ERRORS:
        app.add_exception_handler(error_type, identity_error_handler)
