"""Wiring, observability and the IAM principal adapter of the workflows slice."""

from __future__ import annotations

from uuid import UUID, uuid4

import httpx
import pytest
from fastapicommon.metrics import default_registry
from tests.api.test_lifespan import FakePool

import rag.infrastructure.iam.principals as principals
from rag.application.workflows.bridges import OrphanRestarter, QuotaRunResumer
from rag.domain.shared.errors import DomainError, ErrorCode
from rag.domain.shared.identity import DepartmentRole, ResidencyMode
from rag.infrastructure.config import Settings
from rag.infrastructure.iam.principals import HttpPrincipalDirectory, UnavailablePrincipalDirectory
from rag.infrastructure.observability.workflow_metrics import (
    LABELS,
    METRIC_NAMES,
    P,
    build_workflow_observer,
)
from rag.infrastructure.temporal.client import TemporalWorkflowClient
from rag.infrastructure.temporal.unwired import UnwiredWorkflowClient
from rag.presentation.dependencies import build_container

TENANT = UUID("0a3f0000-0000-7000-8000-000000000001")
USER = UUID("0a3f0000-0000-7000-8000-000000000002")
DEPT = UUID("0a3f0000-0000-7000-8000-0000000000d1")


def container(**settings: object):  # noqa: ANN202
    return build_container(Settings(**settings), FakePool("c", []), FakePool("g", []))  # type: ignore[arg-type]


def test_the_container_reuses_the_generation_building_blocks() -> None:
    c = container()
    w, g = c.workflows, c.generate
    assert w.start._admit is g.admit is w.regenerate_document._admit  # one admission path (ASYNC-1)
    assert w.regenerate_section._engine is g.engine  # A-6 streams through R-3's engine
    assert w.regenerate_section._lifecycle is g.lifecycle
    assert g.complete._mark is not None  # AV-6 marking is wired for A-6's completion
    assert w.approve._a.scope is c.scope.adapter and w.state._scope is c.scope.adapter
    assert w.regenerate_section._refiner._gw is c.retrieval.gateway  # one Gateway client (DEP-2)
    assert w.regenerate_section._refiner._obs is c.retrieval.observer


def test_without_an_address_every_async_entry_fails_closed() -> None:
    w = container().workflows
    assert isinstance(w.client, UnwiredWorkflowClient) and w.client.available is False
    assert w.restarter.available is False and w.resumer.available is False  # nothing is "resumed"


def test_with_an_address_the_sweep_and_the_quota_consumer_are_wired_to_the_engine() -> None:
    w = container(temporal_address="temporal.internal:7233").workflows
    assert isinstance(w.client, TemporalWorkflowClient)
    assert isinstance(w.restarter, OrphanRestarter) and w.restarter.available
    assert isinstance(w.resumer, QuotaRunResumer) and w.resumer.available
    assert w.restarter._workflows is w.client and w.resumer._workflows is w.client


def test_the_instruction_refiner_flag_is_configuration() -> None:
    assert container().workflows.regenerate_section._refiner._enabled is True  # on by default
    off = container(refiner_instruction_enabled=False).workflows
    assert off.regenerate_section._refiner._enabled is False  # the kill switch (§10.5)


def test_the_consumer_wiring_no_longer_uses_the_unwired_stand_in() -> None:
    from rag.presentation.events_dependencies import build_consumer_services

    c = container(temporal_address="temporal.internal:7233")
    services = build_consumer_services(c.settings, c, object())  # type: ignore[arg-type]
    assert services.quota_resume.resumer is c.workflows.resumer


# --- §16 metrics -------------------------------------------------------------------------------
def test_workflow_metric_names_satisfy_the_platform_registry() -> None:
    registry = default_registry()
    for kind, names in METRIC_NAMES.items():
        for name in names:
            registry.check_custom(
                kind, P + name, LABELS.get(name, []), "llm", "retrieval-generation"
            )
    labels = {x for v in LABELS.values() for x in v}
    assert not {x for x in labels if x.endswith(("_id", "_email", "_ip", "_uuid", "_token"))}
    assert not labels & {"tenant", "user", "request", "trace", "subject", "actor"}  # OBS-1
    assert {"scope_revoked_async_total", "regeneration_total"} <= set(METRIC_NAMES["counter"])


async def test_a_disabled_observer_is_safe_and_spans_pass_errors_through() -> None:
    obs = build_workflow_observer(Settings(enable_metrics=False))
    obs.workflow_start("failed")
    obs.workflow_signal("cancel", "sent")
    obs.gate_action("approve")
    obs.scope_revoked()
    obs.regeneration("section", "instruction")
    obs.fact_conflict("dismiss")
    with pytest.raises(ValueError):
        async with obs.span("workflow.x"):
            raise ValueError("boom")


# --- the IAM principal adapter (ASYNC-11) -------------------------------------------------------
def directory(handler) -> HttpPrincipalDirectory:  # type: ignore[no-untyped-def]
    principals.create_platform_http_client = lambda **kw: httpx.AsyncClient(  # type: ignore[assignment]
        transport=httpx.MockTransport(handler), base_url="http://iam.test"
    )
    return HttpPrincipalDirectory("http://iam.test")


async def test_the_principal_is_re_read_from_iam_and_never_taken_from_the_run() -> None:
    original = principals.create_platform_http_client
    try:
        seen: list[httpx.Request] = []

        def ok(req: httpx.Request) -> httpx.Response:
            seen.append(req)
            return httpx.Response(
                200,
                json={
                    "roles": ["tender_user"],
                    "departments": [{"department_id": str(DEPT), "role": "editor"}],
                },
            )

        got = await directory(ok).identity_for(
            TENANT, USER, plan="enterprise", residency_mode="strict"
        )
        assert seen[0].url.path == f"/users/{USER}/memberships"
        assert seen[0].headers["x-tenant-id"] == str(TENANT)
        assert got.roles == {"tender_user"} and got.departments == (DepartmentRole(DEPT, "editor"),)
        assert (got.plan, got.residency_mode, got.principal_id) == (
            "enterprise",
            ResidencyMode.STRICT,
            USER,
        )
    finally:
        principals.create_platform_http_client = original  # type: ignore[assignment]


async def test_an_unknown_principal_is_an_identity_with_no_membership_so_scope_comes_up_empty() -> (
    None
):
    original = principals.create_platform_http_client
    try:
        got = await directory(lambda r: httpx.Response(404)).identity_for(
            TENANT, USER, plan=None, residency_mode="standard"
        )
        assert got.roles == frozenset() and got.departments == ()
    finally:
        principals.create_platform_http_client = original  # type: ignore[assignment]


@pytest.mark.parametrize(
    "handler",
    [
        lambda r: httpx.Response(503),
        lambda r: httpx.Response(200, json=["not", "an", "object"]),
        lambda r: httpx.Response(200, json={"departments": [{"role": "x"}]}),
        lambda r: (_ for _ in ()).throw(httpx.ConnectError("refused")),
    ],
)
async def test_an_outage_refuses_instead_of_building_an_empty_identity(handler) -> None:  # type: ignore[no-untyped-def]
    original = principals.create_platform_http_client
    try:
        with pytest.raises(DomainError) as exc:
            await directory(handler).identity_for(
                TENANT, uuid4(), plan=None, residency_mode="standard"
            )
        assert exc.value.code is ErrorCode.SCOPE_STORE_UNAVAILABLE
    finally:
        principals.create_platform_http_client = original  # type: ignore[assignment]


async def test_without_an_iam_url_nothing_is_re_read() -> None:
    with pytest.raises(DomainError) as exc:
        await UnavailablePrincipalDirectory().identity_for(
            TENANT, USER, plan=None, residency_mode="standard"
        )
    assert exc.value.code is ErrorCode.SCOPE_STORE_UNAVAILABLE
