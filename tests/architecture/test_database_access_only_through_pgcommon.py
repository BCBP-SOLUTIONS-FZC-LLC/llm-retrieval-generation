"""Database connection, configuration and operations go through ``pgcommon`` and nothing else.

No module imports a database driver or ORM (the driver, asyncpg, sits under ``pgcommon``);
``pgcommon`` is imported only by infrastructure and presentation; and the one place that builds a
pool's ``Config`` is ``persistence/pools.py`` (``pool_config``). Errors are classified with
``pgcommon``'s helpers (``is_unique_violation``, ``constraint_name``, ...), never by driver type.
"""

from __future__ import annotations

import ast

from tests.architecture.test_dependency_rules import imports, load_sources, under

DB_DRIVERS = ("asyncpg", "psycopg", "psycopg2", "psycopg_pool", "sqlalchemy", "aiopg", "databases")
POOLS = "rag.infrastructure.persistence.pools"


def test_no_module_imports_a_database_driver_or_orm() -> None:
    offenders = [
        f"{module} imports {target}"
        for module, source in load_sources().items()
        for target in sorted(set(imports(module, source)))
        if any(under(target, driver) for driver in DB_DRIVERS)
    ]
    assert offenders == []


def test_pgcommon_is_imported_only_by_infrastructure_and_presentation() -> None:
    offenders = [
        f"{module} imports {target}"
        for module, source in load_sources().items()
        if module.startswith(("rag.domain", "rag.application", "rag.corpusscope"))
        for target in sorted(set(imports(module, source)))
        if under(target, "pgcommon")
    ]
    assert offenders == []


def _builds_a_pgcommon_config(source: str) -> bool:
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.ImportFrom) and node.module == "pgcommon":
            if any(alias.name == "Config" for alias in node.names):
                return True
        if isinstance(node, ast.Attribute) and node.attr == "Config":
            if isinstance(node.value, ast.Name) and node.value.id == "pgcommon":
                return True
    return False


def test_only_pool_config_builds_a_pgcommon_config() -> None:
    """One factory, so tuning, PgBouncer mode and the tenant GUC provider cannot drift per pool."""
    offenders = [
        module
        for module, source in load_sources().items()
        if module != POOLS and _builds_a_pgcommon_config(source)
    ]
    assert offenders == []
    assert _builds_a_pgcommon_config(load_sources()[POOLS])  # the rule is not vacuous


def test_the_checker_catches_a_stray_config() -> None:
    assert _builds_a_pgcommon_config("from pgcommon import Config, Pool")
    assert _builds_a_pgcommon_config("import pgcommon\nx = pgcommon.Config(dsn='')")
    assert not _builds_a_pgcommon_config("from pgcommon import Pool")
