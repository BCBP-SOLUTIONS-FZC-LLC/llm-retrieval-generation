"""A stand-in for a module's structured logger, so a test can assert on what was logged.

``log = get_logger(__name__)`` modules are patched with ``monkeypatch.setattr(module, "log", rec)``;
this is deterministic, unlike pytest's ``caplog`` (whether it sees structlog events depends on
whether the platform logger was configured earlier in the process).
"""

from __future__ import annotations

from typing import Any


class RecordingLog:
    def __init__(self) -> None:
        self.records: list[tuple[str, str, dict[str, Any]]] = []

    def _record(self, level: str, event: str, fields: dict[str, Any]) -> None:
        self.records.append((level, event, fields))

    def debug(self, event: str, *args: Any, **fields: Any) -> None:
        self._record("debug", event, fields)

    def info(self, event: str, *args: Any, **fields: Any) -> None:
        self._record("info", event, fields)

    def warning(self, event: str, *args: Any, **fields: Any) -> None:
        self._record("warning", event, fields)

    def error(self, event: str, *args: Any, **fields: Any) -> None:
        self._record("error", event, fields)

    def exception(self, event: str, *args: Any, **fields: Any) -> None:
        self._record("error", event, fields)

    def find(self, event: str, level: str | None = None) -> list[dict[str, Any]]:
        """The fields of every record of ``event`` (at ``level`` when given)."""
        return [f for lv, ev, f in self.records if ev == event and level in (None, lv)]
