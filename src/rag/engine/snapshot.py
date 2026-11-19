"""The recorded shape of the engine's public API (workers LLD WB-4).

``python -m rag.engine.snapshot`` prints it; ``engine_api_v1.json`` beside this file is the
committed copy. A contract test here and in ``llm-workers`` compares the two, so the shape cannot
move without a version change.
"""

from __future__ import annotations

import dataclasses
import inspect
import json
from typing import Any

import llm_models.workers as plain
import rag.engine as engine


def _types(module: Any) -> dict[str, dict[str, str]]:
    return {
        name: {f.name: str(f.type) for f in dataclasses.fields(obj)}
        for name, obj in sorted(vars(module).items())
        if inspect.isclass(obj)
        and dataclasses.is_dataclass(obj)
        and obj.__module__ == module.__name__
    }


def snapshot() -> dict[str, Any]:
    """Function signatures, the engine's errors, and the types with their fields."""
    functions: dict[str, str] = {}
    errors: list[str] = []
    engine_types: dict[str, dict[str, str]] = {}
    for name in sorted(engine.__all__):
        obj = getattr(engine, name)
        if inspect.isclass(obj) and issubclass(obj, Exception):
            errors.append(name)
        elif inspect.isclass(obj) and dataclasses.is_dataclass(obj):
            engine_types[name] = {f.name: str(f.type) for f in dataclasses.fields(obj)}
        elif inspect.isfunction(obj):
            functions[name] = str(inspect.signature(obj))
    return {
        "engine_api_version": engine.ENGINE_API_VERSION,
        "functions": functions,
        "errors": errors,
        "engine_types": engine_types,
        "plain_types": _types(plain),
    }


if __name__ == "__main__":
    print(json.dumps(snapshot(), indent=2, sort_keys=True))
