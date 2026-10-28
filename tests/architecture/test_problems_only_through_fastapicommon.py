"""RFC 9457 problem bodies are built by ``fastapicommon`` only, never by hand."""

from __future__ import annotations

import ast

from tests.architecture.test_dependency_rules import load_sources

MEMBERS = {"errorCode", "traceId", "validationErrors", "application/problem+json"}


def _hand_built(source: str) -> list[str]:
    return [
        n.value
        for n in ast.walk(ast.parse(source))
        if isinstance(n, ast.Constant) and isinstance(n.value, str) and n.value in MEMBERS
    ]


def test_no_module_spells_out_a_problem_member() -> None:
    assert {m: h for m, s in load_sources().items() if (h := _hand_built(s))} == {}


def test_the_checker_catches_a_hand_built_body() -> None:
    assert _hand_built('x = {"errorCode": "A", "status": 500}')
    assert not _hand_built("x = {'status': 500}")
