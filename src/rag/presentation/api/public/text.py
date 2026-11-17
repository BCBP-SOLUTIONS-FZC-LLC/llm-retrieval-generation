"""Free-text checks shared by every request model and path segment (#80).

Postgres refuses a NUL character only after the work has started, which surfaced as a 500 or a
503. Refuse it at the edge as ``400 VALIDATION_FAILED``. ``Text`` also refuses blank text: use it
only where the field is required and already has ``min_length=1``.
"""

from __future__ import annotations

from typing import Annotated

from pydantic import AfterValidator


def _no_nul(value: str) -> str:
    if "\x00" in value:
        raise ValueError("must not contain a NUL character")
    return value


def _text(value: str) -> str:
    if not _no_nul(value).strip():
        raise ValueError("must contain text")
    return value


NoNul = Annotated[str, AfterValidator(_no_nul)]
Text = Annotated[str, AfterValidator(_text)]
