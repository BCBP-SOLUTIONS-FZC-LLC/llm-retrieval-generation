"""Gateway streaming adapter (§22.1), generation metrics names (§16), pack codec, small adapters."""

from __future__ import annotations

import asyncio
import json
from uuid import UUID

import httpx
import pytest
from fastapicommon.metrics import default_registry
from tests.support_retrieval import DictKV, bundle, section_profile

from rag.application.generation.ports import UserContext, UserContextUnavailable
from rag.application.shared.gateway import (
    GatewayContext,
    GatewayDeadlineExceeded,
    GatewayQuotaRefused,
    GatewayResidencyRefused,
    GatewayUnavailable,
)
from rag.domain.packs.generation import ContentRule, EligibilityDecl, ExtractionSchema, FieldSpec
from rag.domain.packs.profile import DomainProfile, LanguagePack, PackBundle
from rag.domain.shared.enums import EligibilityState, GenerationMode
from rag.domain.shared.errors import DomainError, ErrorCode
from rag.domain.shared.identity import RequestIdentity
from rag.infrastructure.config import Settings
from rag.infrastructure.gateway.client import HttpModelGateway
from rag.infrastructure.iam.llm_context import HttpUserContext
from rag.infrastructure.observability.generation_metrics import (
    LABELS,
    METRIC_NAMES,
    P,
    build_generation_observer,
    ratio_band,
    score_band,
)
from rag.infrastructure.persistence.generation.probes import CorpusTenderGate, PgStoreProbe
from rag.infrastructure.tender.pack_codec import bundle_from_dict, bundle_to_dict

CTX = GatewayContext(plan="pro", residency_mode="strict", preferred_model="m1")
TENANT = UUID("0a3f0000-0000-7000-8000-000000000001")


def gateway(handler) -> HttpModelGateway:  # type: ignore[no-untyped-def]
    gw = HttpModelGateway(Settings())
    gw._client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="http://gw.test"
    )
    return gw


def sse(*frames: object, done: bool = True) -> bytes:
    lines = [f"data: {f if isinstance(f, str) else json.dumps(f)}\n\n" for f in frames]
    return ("".join(lines) + ("data: [DONE]\n\n" if done else "")).encode()


def delta(text: str, model: str = "gen-large") -> dict[str, object]:
    return {"model": model, "choices": [{"delta": {"content": text}}]}


async def collect(gw: HttpModelGateway, **kw: object):  # type: ignore[no-untyped-def]
    return [
        c
        async for c in gw.generate_stream(
            "draft",
            "sys",
            "usr",
            ctx=CTX,
            deadline_seconds=kw.pop("deadline", 5.0),
            **kw,  # type: ignore[arg-type]
        )
    ]


async def test_stream_parses_openai_sse_with_usage_and_serving_tier() -> None:
    seen: dict[str, object] = {}

    def handler(req: httpx.Request) -> httpx.Response:
        seen["body"], seen["headers"] = json.loads(req.content), req.headers
        body = sse(
            delta("Hello "),
            {"choices": []},  # a keep-alive frame with no choices
            delta("world [c-01]."),
            {
                "model": "gen-large",
                "choices": [],
                "usage": {"prompt_tokens": 90, "completion_tokens": 7},
            },
        )
        return httpx.Response(200, content=body, headers={"x-serving-tier": "fallback"})

    chunks = await collect(gateway(handler))
    assert [c.delta for c in chunks] == ["Hello ", "world [c-01].", ""]
    assert chunks[0].model == "gen-large" and chunks[0].serving_tier == "fallback"
    assert (chunks[-1].prompt_tokens, chunks[-1].completion_tokens) == (90, 7)
    body = seen["body"]
    assert isinstance(body, dict) and body["model"] == "draft" and body["stream"] is True
    assert body["stream_options"] == {"include_usage": True}
    assert [m["role"] for m in body["messages"]] == ["system", "user"]  # type: ignore[index]
    headers = seen["headers"]
    assert isinstance(headers, httpx.Headers)
    assert headers["x-plan"] == "pro" and headers["x-preferred-model"] == "m1"  # a hint, unfiltered
    assert headers["x-residency-mode"] == "strict"


async def test_a_quota_refusal_raises_on_the_first_read_before_any_delta() -> None:
    gw = gateway(lambda r: httpx.Response(429, headers={"retry-after": "30"}, json={}))
    stream = gw.generate_stream("draft", "s", "u", ctx=CTX, deadline_seconds=5)
    with pytest.raises(GatewayQuotaRefused) as exc:
        await stream.__anext__()
    assert exc.value.retry_after_seconds == 30


async def test_residency_and_outage_refusals_map_before_any_delta() -> None:
    gw = gateway(lambda r: httpx.Response(503, json={"error": {"code": "residency_capacity"}}))
    with pytest.raises(GatewayResidencyRefused):
        await collect(gw)
    with pytest.raises(GatewayUnavailable):
        await collect(gateway(lambda r: httpx.Response(502, text="bad gateway")))


async def test_a_malformed_or_error_frame_is_unavailable() -> None:
    for frames in (["{not json"], [{"error": {"message": "boom"}}]):
        with pytest.raises(GatewayUnavailable):
            await collect(gateway(lambda r, f=frames: httpx.Response(200, content=sse(*f))))  # type: ignore[misc]


async def test_a_stream_that_outlives_its_deadline_is_cancelled() -> None:
    class Slow(httpx.AsyncByteStream):
        async def __aiter__(self):  # type: ignore[no-untyped-def]
            yield sse(delta("a"), done=False)
            await asyncio.sleep(0.06)
            yield sse(delta("b"), done=False)

    gw = gateway(lambda r: httpx.Response(200, stream=Slow()))
    with pytest.raises(GatewayDeadlineExceeded):
        await collect(gw, deadline=0.03)


async def test_a_connection_failure_is_unavailable() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    with pytest.raises(GatewayUnavailable):
        await collect(gateway(handler))


# --- metrics (§16): names, kinds and labels validated against the platform registry ------------


def test_generation_metric_names_satisfy_the_platform_registry() -> None:
    registry = default_registry()
    for kind, names in METRIC_NAMES.items():
        for name in names:
            registry.check_custom(
                kind, P + name, LABELS.get(name, []), "llm", "retrieval-generation"
            )
    labels = {x for v in LABELS.values() for x in v}
    assert not {x for x in labels if x.endswith(("_id", "_email", "_ip", "_uuid", "_token"))}
    assert not labels & {"tenant", "user", "request", "trace", "subject", "actor"}  # OBS-1


def test_bands_and_a_disabled_observer() -> None:
    assert [score_band(s) for s in (None, 0.2, 0.5, 0.7, 0.94, 0.95, 1.0)] == [
        "not_computed", "<0.50", "0.50-0.70", "0.70-0.95", "0.70-0.95", ">=0.95", ">=0.95",
    ]  # fmt: skip
    assert [ratio_band(r) for r in (0.0, 0.05, 0.2, 0.4, 0.9)] == [
        "0", "0-0.10", "0.10-0.20", "0.20-0.50", ">0.50",
    ]  # fmt: skip
    obs = build_generation_observer(Settings(enable_metrics=False))
    obs.verified("answer", 1.0)
    obs.delivery("disconnected")
    obs.tokens("draft", "m", "answer", 10)  # none of these raises


# --- pack codec: the cache round-trips the generation fields -----------------------------------


def test_pack_codec_round_trips_generation_fields() -> None:
    profile = section_profile(
        skeleton="Write.", compliance=True, failure_policy="flag", disclaimer_ref="d-1"
    )
    b = bundle(
        profile,
        answer_skeleton="Answer.",
        tenant_overrides="Use UK spelling.",
        eligibility={"x": EligibilityDecl(EligibilityState.NOT_GENERATABLE, "legal")},
        default_eligibility=EligibilityDecl(EligibilityState.PARTIALLY_GENERATABLE, "r"),
        generation_model_profile="gen",
        judge_model_profile="jud",
        content_rules=(ContentRule("r", "x+", ("retrieval",)),),
        extraction_schemas={
            "s": ExtractionSchema(
                "s", "1", (FieldSpec("f", "number", "d", None, 0.0, 9.0, (), True),), "desc"
            )
        },
    )
    again = bundle_from_dict(bundle_to_dict(b))
    assert again.domain_profile == b.domain_profile and again.language_pack == b.language_pack
    sp = again.domain_profile.section_profiles["method_statement"]
    assert (sp.skeleton, sp.compliance, sp.policy, sp.disclaimer_ref) == (
        "Write.",
        True,
        "flag",
        "d-1",
    )
    assert (
        again.domain_profile.eligibility_for("zzz").state is EligibilityState.PARTIALLY_GENERATABLE
    )


def test_a_pack_without_generation_fields_defaults_to_generatable_and_flag() -> None:
    dp = DomainProfile("r", "v", "t", section_profile())
    assert dp.eligibility_for("anything").state is EligibilityState.GENERATABLE
    assert section_profile().policy == "flag" and section_profile(compliance=True).policy == "block"
    _ = (LanguagePack, PackBundle, GenerationMode)


# --- admission probes --------------------------------------------------------------------------


class Conn:
    def __init__(self, value: object) -> None:
        self.value = value

    async def fetchval(self, sql: str, *args: object) -> object:
        return self.value


class Runner:
    def __init__(
        self, value: object = "off", error: Exception | None = None, delay: float = 0
    ) -> None:
        self.conn, self.error, self.delay = Conn(value), error, delay

    async def run_in_tx(self, fn, *, read_only: bool = False):  # type: ignore[no-untyped-def]
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.error:
            raise self.error
        return await fn(self.conn)


async def test_the_store_probe_refuses_a_read_only_or_unreachable_or_slow_store() -> None:
    await PgStoreProbe(Runner("off")).require_writable()  # type: ignore[arg-type]
    for runner in (Runner("on"), Runner(error=OSError("down")), Runner(delay=0.3)):
        with pytest.raises(DomainError) as exc:
            await PgStoreProbe(runner).require_writable()  # type: ignore[arg-type]
        assert exc.value.code is ErrorCode.ARTEFACT_STORE_UNWRITABLE


async def test_the_tender_gate_refuses_a_closed_tender() -> None:
    await CorpusTenderGate(Runner(None)).require_open(TENANT)  # type: ignore[arg-type]
    with pytest.raises(DomainError) as exc:
        await CorpusTenderGate(Runner(1)).require_open(TENANT)  # type: ignore[arg-type]
    assert exc.value.code is ErrorCode.TENDER_CLOSED


# --- llm-context (§22.5, §14.3) ----------------------------------------------------------------

IDENT = RequestIdentity(TENANT, UUID(int=2))


def llm_context(handler, cache: DictKV | None = None) -> HttpUserContext:  # type: ignore[no-untyped-def]
    import rag.infrastructure.iam.llm_context as mod

    ctx = HttpUserContext("http://iam.test", cache)
    mod.create_platform_http_client = lambda **kw: httpx.AsyncClient(  # type: ignore[assignment]
        transport=httpx.MockTransport(handler), base_url="http://iam.test"
    )
    return ctx


async def test_llm_context_builds_layer_3_caches_it_and_degrades_on_failure() -> None:
    import rag.infrastructure.iam.llm_context as mod

    original = mod.create_platform_http_client
    try:
        calls: list[str] = []

        def ok(req: httpx.Request) -> httpx.Response:
            calls.append(req.url.path)
            assert req.headers["x-user-id"] == str(IDENT.principal_id)
            return httpx.Response(
                200,
                json={"display_name": "Alice", "job_title": "Engineer", "writing_tone": "formal",
                      "templates": ["t1"], "context_documents": ["d1"], "preferred_model": "gen-xl",
                      "credentials": {"certifications": "P.Eng"}, "locale": "en-US"},
            )  # fmt: skip

        cache = DictKV()
        provider = llm_context(ok, cache)
        got = await provider.get(IDENT)
        assert got == UserContext(
            "Name: Alice\nJob title: Engineer\nPreferred tone: formal\nTemplate: t1\n"
            "Context document: d1",
            "gen-xl",
        )
        await provider.get(IDENT)
        assert calls == [f"/api/v1/internal/users/{IDENT.principal_id}/llm-context"]
        assert f"{TENANT}:llmctx:" in next(iter(cache.data))  # cached 300 s
        assert "P.Eng" not in next(iter(cache.data.values()))  # credentials never reach the cache

        none = llm_context(lambda r: httpx.Response(404))
        assert await none.get(IDENT) is None
        down = llm_context(lambda r: httpx.Response(500))
        with pytest.raises(UserContextUnavailable):
            await down.get(IDENT)
    finally:
        mod.create_platform_http_client = original  # type: ignore[assignment]
