"""Invariant violations raised by the generation entities (the LLD's table-local rules)."""

from __future__ import annotations


class InvariantViolation(ValueError):
    """An entity was built or moved into a state its LLD invariant forbids (e.g. ``AS-7``)."""

    def __init__(self, rule: str, message: str) -> None:
        self.rule = rule
        super().__init__(f"{rule}: {message}")
