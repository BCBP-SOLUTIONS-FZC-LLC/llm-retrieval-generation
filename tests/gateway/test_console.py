"""The gateway console (http://localhost:8090/console): every task class, through the gateway.

Needs ``make gateway-up``. These drive the console's own endpoint, which sends the request our
client would send to the real gateway and reports what was sent and what came back.
"""

from __future__ import annotations

import json
import os

import httpx
import pytest

pytestmark = pytest.mark.gateway

CONSOLE = os.environ.get("MODEL_STUB_URL", "http://localhost:8090")
TENANT = "11111111-1111-1111-1111-111111111111"
CHAT = ("judge", "extract", "refine-query", "refine-instruction")


@pytest.fixture(autouse=True)
def _running() -> None:
    try:
        httpx.get(f"{CONSOLE}/health", timeout=2).raise_for_status()
        httpx.post(
            "http://localhost:1975/v1/embeddings",
            json={"model": "embed", "input": ["ping"]},
            timeout=5,
        ).raise_for_status()
    except httpx.HTTPError as exc:
        pytest.skip(f"gateway or model stub not running (make gateway-up): {exc}")


def call(task: str, text: str = "hello", **extra: str) -> httpx.Response:
    payload = {"task": task, "text": text, "tenant": TENANT, "plan": "pro", **extra}
    return httpx.post(f"{CONSOLE}/console/call", json=payload, timeout=40)


def test_the_page_is_served_and_lists_every_task_class() -> None:
    page = httpx.get(f"{CONSOLE}/console", timeout=5)
    assert page.status_code == 200 and "text/html" in page.headers["content-type"]
    for task in (
        "embed",
        "rerank",
        "draft",
        "judge",
        "extract",
        "refine-query",
        "refine-instruction",
    ):
        assert f'{{t:"{task}"' in page.text


def test_embed_goes_to_the_openai_embeddings_route() -> None:
    body = call("embed", "payment terms").json()
    assert body["status"] == 200 and body["sent"]["url"].endswith("/v1/embeddings")
    assert len(body["answer"]["data"][0]["embedding"]) == 8


def test_rerank_goes_to_the_cohere_route_and_ranks_by_overlap() -> None:
    passages = "the payment terms are net 30\nweather in Dubai\nterms of delivery"
    body = call("rerank", "payment terms", passages=passages).json()
    assert body["status"] == 200 and body["sent"]["url"].endswith("/cohere/v2/rerank")
    results = body["answer"]["results"]
    assert [r["index"] for r in results] == [0, 2, 1]


@pytest.mark.parametrize("task", CHAT)
def test_the_chat_task_classes_reach_the_chat_route_with_their_own_model_name(task: str) -> None:
    body = call(task).json()
    assert body["status"] == 200 and body["sent"]["url"].endswith("/v1/chat/completions")
    assert body["sent"]["body"]["model"] == task and body["answer"]["model"] == task
    assert body["sent"]["body"]["response_format"] == {"type": "json_object"}


def test_draft_streams_text_then_usage() -> None:
    frames: list[str] = []
    with httpx.stream(
        "POST",
        f"{CONSOLE}/console/call",
        json={"task": "draft", "text": "write", "tenant": TENANT},
        timeout=40,
    ) as r:
        events = [b for b in r.read().decode().split("\n\n") if b]
    assert events[0].startswith("event: meta") and events[-1].startswith("event: done")
    for blk in events:
        if blk.startswith("event: frame"):
            frames.append(json.loads(blk.split("data: ", 1)[1]))
    deltas = "".join(
        c.get("delta", {}).get("content", "")
        for f in frames
        if f.startswith("data: {")
        for c in json.loads(f[6:]).get("choices") or []
    )
    assert deltas.strip() == "fake answer from the stub"
    assert any('"usage"' in f and '"completion_tokens": 5' in f for f in frames)


@pytest.mark.parametrize(
    ("trigger", "status"), [("429", 429), ("residency", 503), ("malformed", 200)]
)
def test_make_it_fail_reaches_the_client_as_the_gateway_passes_it_on(
    trigger: str, status: int
) -> None:
    body = call("judge", trigger=trigger).json()
    assert body["status"] == status
    if trigger == "429":
        assert body["response_headers"]["retry-after"] == "7"


def test_an_unknown_task_class_is_a_400() -> None:
    assert call("not-a-task").status_code == 400
