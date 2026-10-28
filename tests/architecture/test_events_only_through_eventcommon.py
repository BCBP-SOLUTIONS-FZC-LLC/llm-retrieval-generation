"""Events, the outbox and message dedup go through ``eventcommon`` and nothing else.

No module talks to SNS or SQS with a raw client (boto3 is S3's, under ``infrastructure/storage``);
no code runs SQL against the library's tables (the outbox, the dead letters, the dedup ledger);
every envelope comes from ``new_envelope``; ``eventcommon`` is imported by infrastructure and
presentation only; and one module, ``infrastructure/events/config.py``, builds the relay,
publisher and consumer configurations.
"""

from __future__ import annotations

import ast
import re

from tests.architecture.test_dependency_rules import imports, load_sources, under

AWS_SDKS = ("boto3", "botocore", "aioboto3", "aiobotocore")
S3_HOME = "rag.infrastructure.storage"
LIBRARY_TABLES = re.compile(
    r"\b(outbox_events|outbox_dead_letters|outbox_migrations|processed_events|inbox_migrations)\b"
)
EVENTS_CONFIG = "rag.infrastructure.events.config"
CONFIG_CLASSES = {"RunnerConfig", "SQSConfig", "SNSConfig"}


def test_no_module_uses_a_raw_aws_client_outside_the_s3_store() -> None:
    offenders = [
        f"{module} imports {target}"
        for module, source in load_sources().items()
        if not module.startswith(S3_HOME)
        for target in sorted(set(imports(module, source)))
        if any(under(target, sdk) for sdk in AWS_SDKS)
    ]
    assert offenders == []


def _prose(tree: ast.AST) -> set[int]:
    """Strings standing alone as a statement: docstrings and attribute docs, never executed."""
    return {
        id(node.value)
        for node in ast.walk(tree)
        if isinstance(node, ast.Expr)
        and isinstance(node.value, ast.Constant)
        and isinstance(node.value.value, str)
    }


def _strings_naming_library_tables(source: str) -> list[str]:
    tree = ast.parse(source)
    skip = _prose(tree)
    return [
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and id(node) not in skip
        and LIBRARY_TABLES.search(node.value)
    ]


def test_no_code_runs_sql_against_the_librarys_tables() -> None:
    """Docstrings and comments may name them; a string the code can execute may not."""
    offenders = {
        module: hits
        for module, source in load_sources().items()
        if (hits := _strings_naming_library_tables(source))
    }
    assert offenders == {}


def test_the_checker_for_library_tables_catches_sql_but_not_prose() -> None:
    assert _strings_naming_library_tables('q = "SELECT 1 FROM processed_events"')
    assert _strings_naming_library_tables('conn.execute("DELETE FROM outbox_events")')
    assert not _strings_naming_library_tables('"""Prunes processed_events."""\nx = 1')
    assert not _strings_naming_library_tables("def f():\n    '''the outbox_events table'''\n")


def test_eventcommon_is_imported_only_by_infrastructure_and_presentation() -> None:
    offenders = [
        f"{module} imports {target}"
        for module, source in load_sources().items()
        if module.startswith(("rag.domain", "rag.application", "rag.corpusscope"))
        for target in sorted(set(imports(module, source)))
        if under(target, "eventcommon")
    ]
    assert offenders == []


def _builds_a_relay_publisher_or_consumer_config(source: str) -> bool:
    for node in ast.walk(ast.parse(source)):
        if (
            isinstance(node, ast.ImportFrom)
            and node.module
            and node.module.startswith("eventcommon")
        ):
            if any(alias.name in CONFIG_CLASSES for alias in node.names):
                return True
    return False


def test_only_the_events_config_module_builds_these_configurations() -> None:
    offenders = [
        module
        for module, source in load_sources().items()
        if module != EVENTS_CONFIG and _builds_a_relay_publisher_or_consumer_config(source)
    ]
    assert offenders == []
    assert _builds_a_relay_publisher_or_consumer_config(load_sources()[EVENTS_CONFIG])


def test_the_config_checker_catches_a_stray_import() -> None:
    assert _builds_a_relay_publisher_or_consumer_config(
        "from eventcommon.outbox import RunnerConfig"
    )
    assert _builds_a_relay_publisher_or_consumer_config(
        "from eventcommon.consumer import SQSConfig, X"
    )
    assert not _builds_a_relay_publisher_or_consumer_config(
        "from eventcommon.consumer import DLQConfig"
    )


def test_envelopes_are_only_made_by_the_library() -> None:
    """``new_envelope`` stamps the id, time and trace; nobody builds ``Envelope(...)`` by hand."""
    offenders = [
        module
        for module, source in load_sources().items()
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.Call) and getattr(node.func, "id", "") == "Envelope"
    ]
    assert offenders == []
