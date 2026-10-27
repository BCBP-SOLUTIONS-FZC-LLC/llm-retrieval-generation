"""Fakes and a wired rig for the R-2, R-3 and R-4 use-case and API tests."""

from __future__ import annotations

import asyncio
import contextlib
import json
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
from dataclasses import dataclass, replace
from typing import Any
from uuid import UUID

from rag.application.events.contracts import PUBLISHED
from rag.application.generation.commands import AppendAuditEntry
from rag.application.generation.extract import ExtractStructured
from rag.application.generation.flow import (
    AdmitGeneration,
    CompleteGeneration,
    GenerationSettings,
    RunLifecycle,
)
from rag.application.generation.generate import (
    GenerateAnswer,
    GenerateSectionDraft,
    GenerationStream,
    ShortFormEngine,
)
from rag.application.generation.ports import UserContext, UserContextUnavailable
from rag.application.shared.events import DomainEvent
from rag.application.shared.gateway import Completion, GatewayContext, StreamChunk
from rag.application.verification.verify import VerificationConfig, VerifySection
from rag.domain.generation.artefact import Artefact
from rag.domain.generation.repositories import CompletionRows, IdempotencyRace
from rag.domain.generation.run import GenerationRun
from rag.domain.generation.section import ArtefactSection
from rag.domain.generation.views import ArtefactView, VersionDetail, VersionSectionDetail
from rag.domain.packs.generation import ExtractionSchema, FieldSpec
from rag.domain.retrieval.sufficiency import RequiredInputRule
from rag.domain.shared.errors import DomainError, ErrorCode
from rag.domain.shared.identity import RequestIdentity
from rag.infrastructure.events.schemas import validate_published
from tests.support_generation import FakeAuditRepo, FixedClock
from tests.support_retrieval import (
    TENANT,
    TENDER,
    USER,
    FakeGateway,
    Rig,
    bundle,
    section_profile,
)

__all__ = ["TENANT", "TENDER", "USER", "GenRig", "section_profile", "bundle", "SCHEMA"]

SCHEMA = ExtractionSchema(
    "dp-civil/summary",
    "2.1.0",
    (FieldSpec("contract_value", "number", "total contract value"), FieldSpec("region", "string")),
    "Tender summary",
)
GOOD_TEXT = (
    "Crane erection access routes are specified [c-01]. ",
    "Access routes need crane erection [c-02].",
)


class Binder:
    """``TenantBinder`` recording what is bound; ``current`` is the tenant inside the block."""

    def __init__(self) -> None:
        self.current: UUID | None = None
        self.bound: list[UUID] = []

    @contextlib.contextmanager
    def bind(self, tenant_id: UUID) -> Iterator[None]:
        self.current = tenant_id
        self.bound.append(tenant_id)
        try:
            yield
        finally:
            self.current = None


class BoundTx:
    """``TransactionRunner`` that records which tenant was bound when each transaction ran."""

    def __init__(self, binder: Binder) -> None:
        self.binder = binder
        self.seen: list[UUID | None] = []
        self.fail_at: int | None = None

    async def run_in_tx[T](
        self, fn: Callable[[Any], Awaitable[T]], *, read_only: bool = False
    ) -> T:
        self.seen.append(self.binder.current)
        if self.fail_at is not None and len(self.seen) == self.fail_at:
            raise RuntimeError("store down")
        return await fn(None)


class Events:
    """``EventPublisher`` that validates against the published schemas, like the outbox."""

    def __init__(self) -> None:
        self.events: list[DomainEvent] = []

    async def publish(self, tx: Any, event: DomainEvent) -> None:
        assert event.type in PUBLISHED
        validate_published(event.type, event.payload)
        self.events.append(event)

    def types(self) -> list[str]:
        return [e.type for e in self.events]


class WriteRepo:
    """In-memory ``GenerationWriteRepository`` with the key race and failures scriptable."""

    def __init__(self) -> None:
        self.artefacts: dict[UUID, Artefact] = {}
        self.runs: dict[UUID, GenerationRun] = {}
        self.sections: dict[UUID, ArtefactSection] = {}
        self.completions: list[CompletionRows] = []
        self.race_once: GenerationRun | None = None
        self.fail_completion = False

    async def find_run_by_key(
        self, tx: Any, tenant_id: UUID, principal_id: UUID, key: str
    ) -> GenerationRun | None:
        return next(
            (
                r
                for r in self.runs.values()
                if (r.tenant_id, r.principal_id, r.idempotency_key)
                == (tenant_id, principal_id, key)
            ),
            None,
        )

    async def release_key(self, tx: Any, run_id: UUID) -> None:
        self.runs[run_id].idempotency_key = None
        self.runs[run_id].request_hash = None

    async def insert_shell(
        self, tx: Any, artefact: Artefact, run: GenerationRun, section: ArtefactSection
    ) -> None:
        if self.race_once is not None:
            self.runs[self.race_once.id] = self.race_once
            self.race_once = None
            raise IdempotencyRace
        if run.idempotency_key and await self.find_run_by_key(
            tx, run.tenant_id, run.principal_id, run.idempotency_key
        ):
            raise IdempotencyRace
        self.artefacts[artefact.id] = replace(artefact)
        self.runs[run.id] = replace(run)
        self.sections[section.id] = replace(section)

    async def save_state(
        self, tx: Any, artefact: Artefact, run: GenerationRun, section: ArtefactSection
    ) -> None:
        self.artefacts[artefact.id] = replace(artefact)
        self.runs[run.id] = replace(run)
        self.sections[section.id] = replace(section)

    async def next_version_numbers(
        self, tx: Any, artefact_id: UUID, section_id: UUID
    ) -> tuple[int, int]:
        mine = [c for c in self.completions if c.artefact_version.artefact_id == artefact_id]
        return len(mine) + 1, len(mine) + 1

    async def insert_completion(self, tx: Any, rows: CompletionRows) -> None:
        if self.fail_completion:
            raise RuntimeError("insert failed")
        self.completions.append(rows)

    # --- test conveniences ---
    @property
    def run(self) -> GenerationRun:
        assert len(self.runs) == 1, self.runs
        return next(iter(self.runs.values()))

    @property
    def artefact(self) -> Artefact:
        return next(iter(self.artefacts.values()))


class Reads:
    """The slice of ``ArtefactReadRepository`` the replay path reads, over ``WriteRepo``."""

    def __init__(self, repo: WriteRepo) -> None:
        self.repo = repo

    async def get_artefact_view(self, tx: Any, artefact_id: UUID) -> ArtefactView | None:
        art = self.repo.artefacts.get(artefact_id)
        if art is None:
            return None
        cur = next(
            (
                c.artefact_version
                for c in self.repo.completions
                if c.artefact_version.id == art.current_version_id
            ),
            None,
        )
        return ArtefactView(art, cur, None)

    async def get_version_detail(
        self, tx: Any, artefact_id: UUID, version_no: int
    ) -> VersionDetail | None:
        for c in self.repo.completions:
            if (
                c.artefact_version.artefact_id == artefact_id
                and c.artefact_version.version_no == version_no
            ):
                section = self.repo.sections[c.section_version.section_id]
                sec = VersionSectionDetail(
                    section, c.section_version, c.groundedness, c.claims, c.completeness
                )
                return VersionDetail(c.artefact_version, (sec,))
        return None


class Store:
    """``ArtefactContentStore`` over a dict."""

    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}
        self.fail_put = False

    def section_key(self, tenant_id: UUID, artefact_id: UUID, section_id: UUID, n: int) -> str:
        return f"tenants/{tenant_id}/artefacts/{artefact_id}/sections/{section_id}/v{n}.md"

    def diagnostic_key(self, tenant_id: UUID, artefact_id: UUID, name: str) -> str:
        return f"tenants/{tenant_id}/artefacts/{artefact_id}/diagnostics/{name}"

    async def put(self, tenant_id: UUID, key: str, data: bytes) -> None:
        if self.fail_put:
            raise RuntimeError("s3 down")
        self.objects[key] = data

    async def get(self, tenant_id: UUID, content_ref: str) -> bytes:
        return self.objects[content_ref]

    async def delete_prefix(self, tenant_id: UUID, artefact_id: UUID | None = None) -> int:
        return 0


class Probe:
    """``StoreProbe``."""

    def __init__(self) -> None:
        self.down = False

    async def require_writable(self) -> None:
        if self.down:
            raise DomainError(ErrorCode.ARTEFACT_STORE_UNWRITABLE)


class Gate:
    """``TenderGate``: the closed tenders."""

    def __init__(self) -> None:
        self.closed: set[UUID] = set()

    async def require_open(self, tender_id: UUID) -> None:
        if tender_id in self.closed:
            raise DomainError(ErrorCode.TENDER_CLOSED)


class UserCtx:
    """``UserContextProvider``."""

    def __init__(self) -> None:
        self.value: UserContext | None = None
        self.down = False

    async def get(self, identity: RequestIdentity) -> UserContext | None:
        if self.down:
            raise UserContextUnavailable
        return self.value


class StreamGateway(FakeGateway):
    """``FakeGateway`` plus a scriptable ``generate_stream`` and judge / extract answers."""

    def __init__(self) -> None:
        super().__init__()
        self.deltas: list[str] = list(GOOD_TEXT)
        self.second: list[str] | None = None
        self.before_first: Exception | None = None
        self.fail_after: tuple[int, Exception] | None = None
        self.hold: asyncio.Event | None = None
        self.stream_calls: list[tuple[str, str, str, float]] = []
        self.entail_score = 0.9
        self.judge_supported = True
        self.judge_corrected: list[str] = []
        self.judge_error: Exception | None = None
        self.judge_calls: list[str] = []
        self.extract_text = json.dumps(
            {"fields": {"contract_value": {"value": 4820000, "chunk_ids": ["c-01"]}}}
        )
        self.extract_error: Exception | None = None
        self.extract_hold: asyncio.Event | None = None

    async def generate_stream(
        self,
        task_class: str,
        system: str,
        user: str,
        *,
        ctx: GatewayContext,
        deadline_seconds: float,
        temperature: float = 0.2,
    ) -> AsyncIterator[StreamChunk]:
        self.stream_calls.append((task_class, system, user, temperature))
        if self.before_first is not None:
            raise self.before_first
        deltas = self.second if (temperature > 0.5 and self.second is not None) else self.deltas
        for i, delta in enumerate(deltas):
            if i == 1 and self.hold is not None:
                await self.hold.wait()
            if self.fail_after is not None and i == self.fail_after[0]:
                raise self.fail_after[1]
            yield StreamChunk(delta, "gen-large", "primary")
        yield StreamChunk("", "gen-large", "primary", 100, 20)

    async def complete(
        self,
        task_class: str,
        system: str,
        user: str,
        *,
        ctx: GatewayContext,
        deadline_seconds: float,
    ) -> Completion:
        if task_class == "judge":
            self.judge_calls.append(system[:20])
            if self.judge_error is not None:
                raise self.judge_error
            if system.startswith("You check whether"):
                return Completion(json.dumps({"score": self.entail_score}), "judge-small")
            return Completion(
                json.dumps(
                    {"supported": self.judge_supported, "corrected_chunk_ids": self.judge_corrected}
                ),
                "judge-large",
            )
        if task_class == "extract":
            self.completions.append((task_class, system, user))
            if self.extract_hold is not None:
                await self.extract_hold.wait()
            if self.extract_error is not None:
                raise self.extract_error
            return Completion(self.extract_text, "extract-model")
        return await super().complete(
            task_class, system, user, ctx=ctx, deadline_seconds=deadline_seconds
        )


@dataclass
class Frames:
    """What a consumed stream produced."""

    items: list[Any]

    def of(self, kind: type) -> list[Any]:
        return [f for f in self.items if isinstance(f, kind)]


async def drain(stream: GenerationStream) -> Frames:
    """Consume a stream to its end."""
    return Frames([f async for f in stream.events()])


class GenRig(Rig):
    """The retrieval rig plus the generation slice over fakes."""

    def __init__(
        self,
        *,
        profile: Any = None,
        packs_kw: dict[str, Any] | None = None,
        heartbeat: float = 15.0,
        sample: float = 0.0,
    ) -> None:
        self.stream_gw = StreamGateway()
        super().__init__(gateway=self.stream_gw)
        self.packs.bundle = bundle(
            profile or section_profile(),
            **{
                "extraction_schemas": {SCHEMA.ref: SCHEMA},
                "required_inputs": {"method_statement": RequiredInputRule(("drawing",))},
                **(packs_kw or {}),
            },
        )
        self.binder = Binder()
        self.gen_tx = BoundTx(self.binder)
        self.repo, self.events, self.store = WriteRepo(), Events(), Store()
        self.probe, self.gate, self.user_ctx = Probe(), Gate(), UserCtx()
        self.audit_repo = FakeAuditRepo()
        audit = AppendAuditEntry(self.audit_repo)
        clock = FixedClock()
        cfg = GenerationSettings(heartbeat_seconds=heartbeat, idempotency_ttl_seconds=3600)
        self.cfg = cfg
        self.admit = AdmitGeneration(
            self.gen_tx, self.repo, self.provider, self.packs, self.gw, self.probe, self.gate,
            self.events, clock, cfg,
        )  # fmt: skip
        self.lifecycle = RunLifecycle(self.gen_tx, self.repo, audit, clock)
        self.complete = CompleteGeneration(
            self.gen_tx, self.repo, self.store, audit, self.events, clock
        )
        self.verify = VerifySection(VerificationConfig(tier0_sample_rate=sample), rand=lambda: 1.0)
        self.reads = Reads(self.repo)
        self.engine = ShortFormEngine(
            admit=self.admit, lifecycle=self.lifecycle, complete=self.complete,
            pipeline=self.pipeline, recorder=self.recorder, gateway=self.gw, verify=self.verify,
            reads=self.reads, tx=self.gen_tx, binder=self.binder, settings=cfg,
            user_context=self.user_ctx, tokens=self.gw,
        )  # fmt: skip
        self.answer = GenerateAnswer(self.engine, cfg)
        self.draft = GenerateSectionDraft(self.engine, cfg)
        self.extract = ExtractStructured(
            admit=self.admit, lifecycle=self.lifecycle, complete=self.complete,
            pipeline=self.pipeline, recorder=self.recorder, gateway=self.gw, reads=self.reads,
            tx=self.gen_tx, store=self.store, binder=self.binder, settings=cfg,
        )  # fmt: skip

    async def settle(self) -> None:
        """Wait for every detached producer / extraction task to finish."""
        tasks = [*self.engine._tasks, *self.extract._tasks]
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
