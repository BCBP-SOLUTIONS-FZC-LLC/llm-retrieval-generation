"""Pool scope binding, consumer wiring options (LD-75, LD-86) and the events migration text."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest
from eventcommon.codec import GlueDecodeCodec
from eventcommon.mock import MockDLQPublisher
from tests.support_corpus import TENANT
from tests.support_events import Bench

from rag.infrastructure.config import Settings
from rag.infrastructure.events import runtime
from rag.infrastructure.persistence.pools import bind_tenant_scope, request_guc_provider

MIGRATIONS = Path(__file__).resolve().parents[2] / "migrations"
UP = {
    "corpus": (MIGRATIONS / "corpus" / "0003_corpus_events_sweeps.up.sql").read_text(),
    "generation": (MIGRATIONS / "generation" / "0003_generation_events_sweeps.up.sql").read_text(),
}
DOWN = {
    "corpus": (MIGRATIONS / "corpus" / "0003_corpus_events_sweeps.down.sql").read_text(),
    "generation": (
        MIGRATIONS / "generation" / "0003_generation_events_sweeps.down.sql"
    ).read_text(),
}


def test_the_pool_provider_returns_the_bound_tenant_outside_a_request() -> None:
    """Without this every consumer write fails WITH CHECK and every read sees nothing."""
    assert request_guc_provider() is None
    with bind_tenant_scope(TENANT):
        bound = request_guc_provider()
        assert bound is not None and bound.tenant_id == str(TENANT) and not bound.user_id
    assert request_guc_provider() is None


def test_a_scope_cannot_be_bound_from_a_string() -> None:
    """``'system'`` must never reach a ``::uuid`` policy: a UUID is required (LD-19)."""
    with pytest.raises(TypeError), bind_tenant_scope("system"):  # type: ignore[arg-type]
        pass


def test_six_queues_with_glue_decoding_on_the_two_iam_queues_only() -> None:
    names = {q.queue: q for q in runtime.QUEUES}
    assert set(names) == {
        "llm-document-rag-q",
        "domainprofile-llm-invalidate-q",
        "iam-rag-purge-q",
        "iam-rag-membership-q",
        "tender-rag-q",
        "usage-rag-q",
    }
    assert {n for n, q in names.items() if q.glue} == {"iam-rag-purge-q", "iam-rag-membership-q"}
    assert len({q.consumer for q in runtime.QUEUES}) == 6  # one ledger name each (EVT-6)


def consumer_for(queue: str) -> Any:
    spec = next(q for q in runtime.QUEUES if q.queue == queue)
    settings = Settings(**{spec.url_setting: f"https://sqs.example/{queue}"})
    return runtime.build_consumer(
        settings,
        spec,
        Bench().services,
        object(),
        MockDLQPublisher("dlq"),  # type: ignore[arg-type]
    )


def test_every_consumer_forwards_poison_to_its_dlq_at_the_fourth_receive() -> None:
    """LD-86 / LD-75: eventcommon forwards when the receive count *exceeds* the threshold."""
    for q in runtime.QUEUES:
        consumer = consumer_for(q.queue)
        assert consumer._max_receive_count == 3 and consumer._dlq is not None
        assert isinstance(consumer._codec, GlueDecodeCodec) == q.glue


def test_no_url_means_no_consumer_and_no_aws() -> None:
    running = runtime.start_consumers(Settings(), Bench().services, object())  # type: ignore[arg-type]
    assert running.consumers == {} and running.tasks == []


@pytest.mark.parametrize("instance", ["corpus", "generation"])
def test_events_migration_parses_and_is_reversed_by_its_down(instance: str) -> None:
    pglast = pytest.importorskip("pglast")
    assert pglast.parse_sql(UP[instance]) and pglast.parse_sql(DOWN[instance])
    for policy in re.findall(r"CREATE POLICY (\w+) ON (\w+)", UP[instance]):
        assert f"DROP POLICY IF EXISTS {policy[0]} ON {policy[1]};" in DOWN[instance]


@pytest.mark.parametrize("instance", ["corpus", "generation"])
def test_the_sweeper_role_is_read_only_column_limited_and_not_bypassrls(instance: str) -> None:
    sql = "\n".join(line for line in UP[instance].splitlines() if not line.startswith("--"))
    assert "BYPASSRLS" not in sql.upper()
    allowed = {
        "tenant_id",
        "state",
        "changed_at",
        "status",
        "promoted_from",
        "created_at",
        "updated_at",
        "idempotency_key",
        "artefact_id",
    }
    for columns, _table in re.findall(r"GRANT SELECT \(([^)]*)\) ON (\w+) TO rag_sweeper;", sql):
        assert {c.strip() for c in columns.split(",")} <= allowed  # ids and filter columns only
    assert not re.search(r"GRANT (?!SELECT \(|USAGE ON SCHEMA public )[^;]*TO rag_sweeper", sql)
    # every enumeration policy is FOR SELECT, names rag_sweeper alone
    for line in re.findall(r"CREATE POLICY[^;]*;", sql):
        assert "FOR SELECT TO rag_sweeper USING (true)" in line


@pytest.mark.parametrize("instance", ["corpus", "generation"])
def test_outbox_grants_sit_outside_set_role_because_the_library_owns_those_tables(
    instance: str,
) -> None:
    """A GRANT under ``SET ROLE rag_owner`` on a table rag_owner does not own only warns."""
    sql = UP[instance]
    grant = sql.index("GRANT SELECT, INSERT, UPDATE, DELETE ON outbox_events")
    assert sql.rindex("RESET ROLE;", 0, grant) > sql.rindex("SET ROLE rag_owner;", 0, grant)
    assert "outbox_events_ordering_seq" in sql
    if instance == "generation":  # the Worker Pool enqueues, and runs no relay (OBX-3)
        assert "GRANT INSERT ON outbox_events TO rag_activity;" in sql
        assert not re.search(r"(UPDATE|DELETE)[^;]*outbox_events TO rag_activity", sql)


def test_migrate_all_applies_the_outbox_before_the_numbered_sets() -> None:
    import inspect

    from rag.infrastructure.persistence import migrate

    src = inspect.getsource(migrate.migrate_all)
    assert src.index("_apply_outbox") < src.index("migrate_instance")
