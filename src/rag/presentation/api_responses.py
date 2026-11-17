"""The error responses every route declares in the OpenAPI document.

FastAPI declares only the success status and a 422 the app never returns: its validation handler
answers 400 (VALIDATION_FAILED). Every error is an RFC 9457 problem. The statuses below are those
seen on the running service (workbook rows P1-77, P2-96, P3-77, P6-105, CM-78, R5-S1, R7-S1,
R6-S1, R4-S1 name the gaps this closes). Routes not listed in ``EXTRA`` carry the standard set
only: their specific statuses (403, 404, 409, 429 ...) are not declared until they are tested.
The app applies this to its own ``/openapi.json``, so the committed spec and the served one agree.
"""

from __future__ import annotations

import dataclasses
from typing import Any

from fastapi import FastAPI
from fastapicommon.problem import PROBLEM_MEDIA_TYPE, Problem

NO_AUTH = {("get", "/api/internal/models")}
"""C-M1 holds no tenant and answers neither 401 nor 400 (LD-95, API-11)."""

STANDARD = {
    "400": "VALIDATION_FAILED: a request value is out of bounds or malformed.",
    "401": "UNAUTHORIZED: the identity headers are missing, repeated or not a canonical uuid.",
    "500": "INTERNAL_ERROR: an unclassified failure (the message is never returned).",
    "503": "A dependency this route needs cannot answer (the problem's errorCode names it).",
}

EXTRA: dict[tuple[str, str], dict[str, str]] = {
    ("get", "/api/v1/collections/{collection_id}"): {
        "404": "COLLECTION_NOT_FOUND (absent or not granted)."
    },
    ("get", "/api/v1/collections/{collection_id}/grants"): {
        "403": "The caller is not a tenant admin.",
        "404": "COLLECTION_NOT_FOUND (absent or not granted).",
    },
    ("put", "/api/v1/collections/{collection_id}/retention"): {
        "201": "The first policy was created.",
        "403": "Not a tenant admin, or a platform collection.",
        "404": "COLLECTION_NOT_FOUND (absent or not granted).",
        "409": "OPTIMISTIC_LOCK_CONFLICT (the current version is in recordVersion).",
    },
    ("post", "/api/v1/search"): {
        "206": "A degraded retrieval: degradation_markers names each stage.",
        "403": "GRANT_LEVEL_INSUFFICIENT.",
        "404": "COLLECTION_NOT_FOUND (absent or not granted).",
    },
    ("get", "/api/v1/retrieval-sets/{retrieval_set_id}"): {
        "404": "RETRIEVAL_SET_NOT_FOUND (absent, expired or out of scope)."
    },
    ("post", "/api/v1/required-input-checks"): {
        "404": "COLLECTION_NOT_FOUND (absent or not granted)."
    },
    ("post", "/api/v1/classification/propose"): {
        "413": "USER_INPUT_OVER_BUDGET: the Gateway says the document is too large for the model.",
        "429": "QUOTA_EXHAUSTED, with Retry-After where the Gateway sent one.",
    },
    ("post", "/api/v1/extractions"): {
        "206": "A degraded retrieval: degradation_markers names each stage.",
        "403": "GRANT_LEVEL_INSUFFICIENT, TENANT_NOT_ACTIVE or GENERATION_NOT_PERMITTED.",
        "404": "COLLECTION_NOT_FOUND (absent or not granted).",
        "409": "IDEMPOTENCY_KEY_CONFLICT or TENDER_CLOSED.",
        "422": "PACK_INVALID, VERIFICATION_PAIR_INVALID or EXTRACTION_SCHEMA_VIOLATION.",
        "429": "QUOTA_EXHAUSTED, with Retry-After where the Gateway sent one.",
    },
    ("post", "/api/v1/internal/collections/{collection_id}/migration-result"): {
        "404": "COLLECTION_NOT_FOUND.",
        "409": "OPTIMISTIC_LOCK_CONFLICT or the migration state does not allow the result.",
    },
}


def _problem_schema() -> dict[str, Any]:
    """The wire shape of ``fastapicommon``'s ``Problem``, derived from its own fields.

    The library parses a problem into snake_case fields and writes it in camelCase; deriving the
    names here means this module spells out no problem member by hand (the architecture rule).
    """
    json_types = {"str": "string", "int": "integer"}
    properties: dict[str, Any] = {}
    for field in dataclasses.fields(Problem):
        if field.name == "extensions":  # every other top-level member the server adds
            continue
        head, *tail = field.name.split("_")
        name = head + "".join(part.title() for part in tail)
        if field.name == "validation_errors":
            properties[name] = {
                "type": "array",
                "items": {"type": "object", "properties": {"field": {}, "message": {}}},
            }
        else:
            kind = str(field.type).split(" ")[0]
            properties[name] = {"type": json_types.get(kind, "string")}
    return {
        "type": "object",
        "description": f"RFC 9457 problem, media type {PROBLEM_MEDIA_TYPE}.",
        "properties": properties,
        "required": ["type", "title", "status"],
        "additionalProperties": True,
    }


def declare_responses(doc: dict[str, Any]) -> None:
    """Replace the default 422 and add the standard and per-route error responses in place."""
    schemas = doc.setdefault("components", {}).setdefault("schemas", {})
    schemas["Problem"] = _problem_schema()

    def problem(description: str) -> dict[str, Any]:
        content = {PROBLEM_MEDIA_TYPE: {"schema": {"$ref": "#/components/schemas/Problem"}}}
        return {"description": description, "content": content}

    for path, item in doc["paths"].items():
        for method, op in item.items():
            responses = op["responses"]
            responses.pop("422", None)  # the app answers a validation failure with 400
            key = (method, path)
            takes_input = bool(op.get("parameters") or op.get("requestBody"))
            for status, text in STANDARD.items():
                if key in NO_AUTH and status in {"400", "401"}:
                    continue
                if status == "400" and not takes_input:
                    continue
                responses.setdefault(status, problem(text))
            for status, text in EXTRA.get(key, {}).items():
                if status in {"201", "206"}:  # a success: the same body as the 200
                    responses[status] = {**responses.get("200", {}), "description": text}
                else:
                    responses[status] = problem(text)
    for name in ("HTTPValidationError", "ValidationError"):
        schemas.pop(name, None)


def install(app: FastAPI) -> None:
    """Make ``app.openapi()`` carry the declared responses (computed once, like FastAPI's own)."""
    original = app.openapi

    def openapi() -> dict[str, Any]:
        if app.openapi_schema is None:
            declare_responses(original())  # ``original`` stores the schema it returns
        return app.openapi_schema or {}

    app.openapi = openapi  # type: ignore[method-assign]
