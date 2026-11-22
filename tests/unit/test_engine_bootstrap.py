"""``build_engine``: the composition root wires the same objects the API wires, with no app."""

from __future__ import annotations

from uuid import UUID, uuid4

import pytest
from tests.api.test_lifespan import FakePool

from rag.engine import bootstrap
from rag.engine.blocks import Blocks
from rag.infrastructure.config import ConfigError, Settings
from rag.infrastructure.persistence.pools import bind_tenant_scope

TENANT = UUID("0a3f0000-0000-7000-8000-000000000001")


@pytest.fixture
def pools(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    log: list[str] = []

    async def corpus(settings: Settings) -> FakePool:
        return FakePool("corpus", log)

    async def generation(settings: Settings) -> FakePool:
        return FakePool("generation", log)

    monkeypatch.setattr(bootstrap, "create_corpus_pool", corpus)
    monkeypatch.setattr(bootstrap, "create_generation_pool", generation)
    return log


async def test_the_engine_is_built_over_both_pools_and_closes_them(pools: list[str]) -> None:
    engine = await bootstrap.build_engine(Settings(environment="test"))
    s = engine.services
    assert isinstance(s.blocks, Blocks) and s.review_gate_days == 30
    assert s.blocks.pipeline is engine.container.retrieval.pipeline  # one pipeline, not a copy
    assert s.blocks.extractor is engine.container.generate.extract
    assert s.tx is engine.container.generation_tx
    await engine.aclose()
    assert sorted(pools) == ["close corpus", "close generation"]


async def test_the_scope_binds_the_tenant_and_decodes_the_start_argument(pools: list[str]) -> None:
    engine = await bootstrap.build_engine(Settings(environment="test"))
    body = {
        "tenant_id": str(TENANT),
        "principal_id": str(uuid4()),
        "artefact_id": str(uuid4()),
        "generation_run_id": str(uuid4()),
        "capability": "technical_proposal",
        "requested_mode": "assistance",
        "residency_mode": "standard",
        "domain_profile_version": "dp-1",
        "language_pack_version": "lp-1",
    }
    async with engine.run_scope(body) as ctx:
        assert ctx.tenant_id == TENANT and str(ctx.run_id) == body["generation_run_id"]
        assert ctx.services is engine.services
    with pytest.raises(KeyError):  # a malformed argument is refused before any statement
        async with engine.run_scope({"tenant_id": str(TENANT)}):
            pass
    assert bind_tenant_scope  # the same binder consumers and sweeps use
    await engine.aclose()


async def test_a_wrong_pgbouncer_mode_refuses_to_start_before_any_pool_opens(
    pools: list[str],
) -> None:
    """The tenant GUC would be session-scoped and leak between clients (RLS-6, ISO-11)."""
    with pytest.raises(ConfigError, match="PG_BOUNCER_MODE"):
        await bootstrap.build_engine(Settings(environment="test", pg_bouncer_mode="True"))
    assert pools == []
