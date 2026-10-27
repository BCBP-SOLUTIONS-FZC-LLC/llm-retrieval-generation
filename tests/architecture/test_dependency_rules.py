"""Import-graph rules: Clean Architecture layering and LLD §3.2 DEP-1..5 (ast-based)."""

from __future__ import annotations

import ast
import sys
from collections.abc import Iterator
from pathlib import Path

SRC = Path(__file__).resolve().parents[2] / "src"
STDLIB = set(sys.stdlib_module_names)
MODULES = {"corpus", "retrieval", "generation", "verification", "workflows", "packs", "corpusscope"}
VENDORED_THIRD_PARTY = {"pydantic", "qdrant_client"}
HTTP_CLIENTS = {"httpx", "requests", "aiohttp", "urllib3", "fastapicommon.propagation"}
HTTP_CLIENT_HOMES = (
    "rag.infrastructure.gateway",
    "rag.infrastructure.tender",
    "rag.infrastructure.iam",
)
GATEWAY_URL_HOMES = (
    "rag.infrastructure.gateway",
    "rag.infrastructure.config",
    "rag.presentation.dependencies",
)

Sources = dict[str, str]
"""Dotted module name -> source text."""


def load_sources() -> Sources:
    """Every module under ``src/rag``, keyed by dotted name."""
    out: Sources = {}
    for path in sorted((SRC / "rag").rglob("*.py")):
        parts = list(path.relative_to(SRC).with_suffix("").parts)
        if parts[-1] == "__init__":
            parts.pop()
        out[".".join(parts)] = path.read_text(encoding="utf-8")
    return out


def imports(module: str, source: str) -> Iterator[str]:
    """Absolute dotted names imported by ``module`` (relative imports resolved)."""
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            yield from (alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                base = module.split(".")[: -node.level]
                yield ".".join([*base, *([node.module] if node.module else [])])
            elif node.module:
                yield node.module
                # `from rag.domain import shared` imports the submodule `rag.domain.shared`.
                yield from (f"{node.module}.{alias.name}" for alias in node.names)


def layer_of(module: str) -> str | None:
    """``domain`` / ``application`` / ``infrastructure`` / ``presentation`` / ``corpusscope``."""
    parts = module.split(".")
    return parts[1] if len(parts) > 1 and parts[0] == "rag" else None


def module_of(dotted: str) -> str | None:
    """The LLD §3.1 module a ``rag.*`` dotted name belongs to, if any."""
    parts = dotted.split(".")
    if parts[0] != "rag":
        return None
    return next((p for p in parts[1:] if p in MODULES), None)


def under(name: str, prefixes: tuple[str, ...] | str) -> bool:
    """Whether ``name`` is one of the dotted prefixes or inside one."""
    prefixes = (prefixes,) if isinstance(prefixes, str) else prefixes
    return any(name == p or name.startswith(p + ".") for p in prefixes)


def allowed_layer_import(layer: str, target: str) -> bool:
    """Layer rules for one import ``target`` made from ``layer``."""
    top = target.split(".")[0]
    if layer == "domain":  # stdlib + domain only
        return top in STDLIB or under(target, "rag.domain")
    if layer == "application":  # stdlib + domain + application only
        return top in STDLIB or under(target, ("rag.domain", "rag.application"))
    if layer == "infrastructure":  # anything but the outer layer
        return not under(target, "rag.presentation")
    if layer == "corpusscope":  # vendored: stdlib, pydantic, qdrant_client and itself only
        return top in STDLIB or top in VENDORED_THIRD_PARTY or under(target, "rag.corpusscope")
    return True


def violations(sources: Sources) -> list[str]:
    """All layering and DEP-1..4 violations across ``sources``."""
    found: list[str] = []
    for module, source in sources.items():
        layer = layer_of(module)
        mod = module_of(module)
        for target in sorted(set(imports(module, source))):
            if layer and not allowed_layer_import(layer, target):
                found.append(f"layering: {module} ({layer}) imports {target}")
            if under(target, "qdrant_client") and not under(module, "rag.corpusscope"):
                found.append(f"DEP-1: {module} imports {target}")
            if any(under(target, c) for c in HTTP_CLIENTS) and not under(module, HTTP_CLIENT_HOMES):
                found.append(f"DEP-2: {module} imports HTTP client {target}")
            tgt = module_of(target)
            if mod == "corpus" and tgt in {"retrieval", "generation", "verification"}:
                found.append(f"DEP-3: {module} imports {target}")
            if mod == "verification" and tgt == "retrieval":
                found.append(f"DEP-4: {module} imports {target}")
        if "gateway_base_url" in source and not under(module, GATEWAY_URL_HOMES):
            found.append(f"DEP-2: {module} reads the gateway base URL")
    return found


def test_real_tree_has_no_violations() -> None:
    """The service's own import graph satisfies every rule."""
    assert violations(load_sources()) == []


def test_tree_is_not_empty() -> None:
    """Guard against the walker silently scanning nothing."""
    sources = load_sources()
    assert "rag.domain.shared.errors" in sources and "rag.presentation.main" in sources


def test_domain_and_application_never_import_infrastructure() -> None:
    """Domain and application are independent of every outer layer (explicit, redundant)."""
    for module, source in load_sources().items():
        if layer_of(module) in {"domain", "application"}:
            bad = [
                t
                for t in imports(module, source)
                if under(t, ("rag.infrastructure", "rag.presentation"))
            ]
            assert not bad, f"{module} imports {bad}"


def test_domain_imports_only_stdlib() -> None:
    """No third-party import (pydantic, fastapi, ...) in the domain."""
    for module, source in load_sources().items():
        if layer_of(module) == "domain":
            bad = [t for t in imports(module, source) if t.split(".")[0] not in STDLIB | {"rag"}]
            assert not bad, f"{module} imports {bad}"


# --- the checker itself must catch each violation -----------------------------------------


def test_checker_catches_layer_violations() -> None:
    """Synthetic sources trip each layering rule."""
    bad = violations(
        {
            "rag.domain.x": "import pydantic",
            "rag.domain.y": "from rag.infrastructure import config",
            "rag.application.z": "from rag.infrastructure.config import Settings",
            "rag.infrastructure.w": "from rag.presentation import main",
            "rag.corpusscope.v": "from rag.domain.shared import enums",
        }
    )
    assert len(bad) >= 5


def test_checker_catches_dep_1_to_4() -> None:
    """DEP-1..4 each fire on a synthetic offender and stay quiet on a legitimate one."""
    assert violations({"rag.infrastructure.persistence.q": "import qdrant_client"})
    assert not violations({"rag.corpusscope.q": "import qdrant_client"})
    assert violations({"rag.application.retrieval.r": "import httpx"}) == [
        "layering: rag.application.retrieval.r (application) imports httpx",
        "DEP-2: rag.application.retrieval.r imports HTTP client httpx",
    ]
    assert not violations({"rag.infrastructure.gateway.c": "import httpx"})
    assert violations({"rag.application.corpus.u": "from rag.application.retrieval import x"})
    assert violations({"rag.application.verification.u": "from rag.application.retrieval import x"})
    assert not violations(
        {"rag.application.generation.u": "from rag.application.retrieval import x"}
    )
    assert violations({"rag.application.generation.u": "gateway_base_url"})


def test_corpusscope_is_standalone() -> None:
    """The vendored library imports only stdlib, pydantic, qdrant_client and itself (§3.1)."""
    assert (
        violations({"rag.corpusscope.a": "import hashlib\nimport pydantic\nimport qdrant_client"})
        == []
    )
    assert violations({"rag.corpusscope.a": "import redis"})
    assert violations({"rag.corpusscope.a": "import httpx"})
    assert violations({"rag.corpusscope.a": "from rag.infrastructure import config"})
    assert violations({"rag.corpusscope.a": "from rag.domain.shared import enums"})
    scanned = [m for m in load_sources() if layer_of(m) == "corpusscope"]
    assert len(scanned) >= 8


def test_only_corpusscope_imports_qdrant_client() -> None:
    """DEP-1 against the real tree, stated directly."""
    users = {
        m
        for m, src in load_sources().items()
        if any(under(t, "qdrant_client") for t in imports(m, src))
    }
    assert users and all(under(m, "rag.corpusscope") for m in users)


def test_verification_is_real_and_receives_retrieval_only_as_data() -> None:
    """DEP-4 against the real tree: the module exists, so the rule is not vacuous."""
    sources = load_sources()
    verification = [m for m in sources if module_of(m) == "verification"]
    assert {"rag.domain.verification.tiers", "rag.application.verification.verify"} <= set(
        verification
    )
    for module in verification:
        assert not [t for t in imports(module, sources[module]) if module_of(t) == "retrieval"]
