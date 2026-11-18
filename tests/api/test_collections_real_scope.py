"""The collection routes (P-1, P-2) with the REAL scope resolver behind them (#12).

``test_corpus_routes.py`` fixes the visible set by hand. These tests let ``corpusscope`` decide:
department standing, a restricted tender that only a TAC-4 grant opens (never listed by P-1, which
cannot name a tender), the platform reference collection, and the uniform 404 (CG-8).
"""

from __future__ import annotations

from dataclasses import replace
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from tests.support_corpus import FakeCatalogue, FakePoints, World
from tests.support_scope import DEPT_A, DEPT_B, FakeTac4, resolver, row

from rag.corpusscope import PLATFORM
from rag.domain.shared.enums import CollectionStatus as C
from rag.infrastructure.config import Settings
from rag.infrastructure.scope.adapter import CorpusScopeAdapter
from rag.presentation.corpus_dependencies import build_corpus_services, get_corpus_services
from rag.presentation.main import create_app

TENANT = UUID("0a3f0000-0000-7000-8000-000000000001")
USER = "0a3f0000-0000-7000-8000-000000000002"


def headers(dept: UUID = DEPT_A, role: str = "member") -> dict[str, str]:
    return {
        "x-tenant-id": str(TENANT),
        "x-user-id": USER,
        "x-tenant-roles": role,
        "x-departments": f"{dept}:{role}",
    }


class Stack:
    """Fake world rows and the matching ``CollectionRow`` rows, behind the real resolver."""

    def __init__(self, settings: Settings) -> None:
        self.world = World()
        self.tac4 = FakeTac4()
        self.rows: list = []
        self.app: FastAPI = create_app(settings)
        self.client = TestClient(self.app)
        self.settings = settings

    def add(self, *, restricted: bool = False, dept: UUID = DEPT_A, reference: bool = False):  # type: ignore[no-untyped-def]
        """One active collection, in the fake database and as a resolver row of the same id."""
        c = self.world.add_collection(tender_id=uuid4(), restricted=restricted, status=C.ACTIVE)
        r = row("reference" if reference else "tender", restricted=restricted, departments=(dept,))
        self.rows.append(
            replace(r, id=c.id, tender_id=c.tender_id if not reference else r.tender_id)
        )
        return c

    def start(self) -> TestClient:
        res, _ = resolver(self.rows, self.tac4)
        scope = CorpusScopeAdapter(res)
        self.app.dependency_overrides[get_corpus_services] = lambda: build_corpus_services(
            self.settings,
            self.world.tx,
            repos=self.world.repos,
            events=self.world.events,
            scope=scope,
            points=FakePoints(0),
            catalogue=FakeCatalogue(),
        )
        return self.client


@pytest.fixture
def stack(settings: Settings) -> Stack:
    return Stack(settings)


def ids(response) -> list[str]:  # type: ignore[no-untyped-def]
    return [c["id"] for c in response.json()["collections"]]


def test_a_department_member_lists_their_departments_collections_and_an_outsider_none(
    stack: Stack,
) -> None:
    mine = stack.add(dept=DEPT_A)
    stack.add(dept=DEPT_B)  # another department's collection
    client = stack.start()
    got = client.get("/api/v1/collections", headers=headers(DEPT_A))
    assert got.status_code == 200 and ids(got) == [str(mine.id)]
    outsider = client.get("/api/v1/collections", headers=headers(uuid4()))
    assert outsider.status_code == 200 and ids(outsider) == []  # an empty scope is a list, not 404


def test_a_restricted_tender_is_never_listed_and_p2_follows_the_tac4_grant(stack: Stack) -> None:
    open_ = stack.add(dept=DEPT_A)
    restricted = stack.add(restricted=True)
    client = stack.start()
    listing = client.get("/api/v1/collections", headers=headers())
    assert ids(listing) == [str(open_.id)]  # P-1 cannot name the tender, so never lists it (CG-11)
    url = f"/api/v1/collections/{restricted.id}"
    no_grant = client.get(url, headers=headers())
    assert no_grant.status_code == 404 and no_grant.json()["errorCode"] == "COLLECTION_NOT_FOUND"
    absent = client.get(f"/api/v1/collections/{uuid4()}", headers=headers())
    assert absent.status_code == 404
    assert absent.json()["errorCode"] == no_grant.json()["errorCode"]  # CG-8
    stack.tac4.answers[restricted.tender_id] = "view"
    assert client.get(url, headers=headers()).status_code == 200  # the overlay grant opens P-2


def test_the_platform_reference_collection_is_listed_for_every_tenant(stack: Stack) -> None:
    ref = stack.add(reference=True)
    stack.world.state.collections[ref.id] = replace(
        stack.world.state.collections[ref.id], tenant_id=PLATFORM
    )
    client = stack.start()
    got = client.get("/api/v1/collections", headers=headers(uuid4()))  # no department at all
    assert str(ref.id) in ids(got)
