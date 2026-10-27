"""Validation and JSON Schema of the event contracts, derived from the dataclasses (§12.6).

The LLD makes AsyncAPI 3.0 the source of truth and JSON Schema a derivative that is never
hand-written (EVT-9, LD-65). The contracts in ``application.events.contracts`` are this
repository's single description of each payload: this module derives from them the JSON Schema
files committed under ``schemas/events`` (checked for drift by a test) and the check
``OutboxEventPublisher`` runs on every event before it is enqueued. Every object schema declares
``additionalProperties: true`` (LD-65): ``schemagov`` rejects ``false``, and additive change must
stay compatible.
"""

from __future__ import annotations

import json
import re
from functools import cache
from pathlib import Path
from typing import Any

from pydantic import TypeAdapter

from rag.application.events.contracts import CONSUMED, PUBLISHED

SCHEMA_DIR = Path(__file__).resolve().parents[4] / "schemas" / "events"


def schema_stem(event_type: str) -> str:
    """``SectionCompleted`` -> ``section_completed``, as ``schemagov`` derives it."""
    return re.sub(r"(?<!^)(?=[A-Z])", "_", event_type).lower()


@cache
def _adapter(event_type: str, consumed: bool) -> TypeAdapter[Any]:
    return TypeAdapter((CONSUMED if consumed else PUBLISHED)[event_type])


def _open(node: Any) -> None:
    if isinstance(node, dict):
        if node.get("type") == "object":
            node["additionalProperties"] = True
        for value in node.values():
            _open(value)
    elif isinstance(node, list):
        for value in node:
            _open(value)


def json_schema(event_type: str) -> dict[str, Any]:
    """JSON Schema (draft 2020-12) of a published type, every object open (LD-65)."""
    schema = _adapter(event_type, False).json_schema()
    _open(schema)
    schema["title"] = event_type
    return schema


def schema_files() -> dict[str, str]:
    """File name -> text of every published type's schema, stable for diffing."""
    return {
        f"{schema_stem(t)}.json": json.dumps(json_schema(t), indent=2, sort_keys=True) + "\n"
        for t in sorted(PUBLISHED)
    }


def validate_published(event_type: str, payload: dict[str, Any]) -> None:
    """Fail closed: an unregistered type or a payload off its schema raises (EVT-9, EVT-7).

    Run where the event is enqueued, in the state change's transaction, so a contract violation
    rolls the change back loudly instead of surfacing at the relay as a dead letter.
    """
    if event_type not in PUBLISHED:
        raise ValueError(f"{event_type!r} is not a registered event contract (EVT-9)")
    _adapter(event_type, False).validate_python(payload)


def parse_consumed(event_type: str, data: Any) -> Any:
    """The fields this service reads from a consumed payload; extra keys are ignored.

    Raises ``pydantic.ValidationError`` (a ``ValueError``) on a missing or malformed field.
    """
    if event_type not in CONSUMED:
        raise ValueError(f"{event_type!r} is not a consumed event type")
    return _adapter(event_type, True).validate_python(data)


def write_schemas(directory: Path = SCHEMA_DIR) -> None:
    """Regenerate the committed files (``python -m rag.infrastructure.events.schemas``)."""
    directory.mkdir(parents=True, exist_ok=True)
    for name, text in schema_files().items():
        (directory / name).write_text(text, encoding="utf-8")


if __name__ == "__main__":
    write_schemas()
