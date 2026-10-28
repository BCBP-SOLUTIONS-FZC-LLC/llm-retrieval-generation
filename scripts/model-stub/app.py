"""The model stub: a stand-in for the AI models behind the gateway, for local runs and the
gateway contract tests. It is a test double, not a model.

It answers the request shapes the retrieval-generation service sends through the gateway, with
deterministic answers, and it can fail on command. A marker anywhere in the text of a request
(a chat message, an embedding input, a rerank query) triggers the failure:

    [[429]]          rate-limit refusal (429 + retry-after)
    [[residency]]    no in-region capacity (503, error code ``residency_capacity``)
    [[malformed]]    a 200 whose body is not the expected shape
    [[slow:<ms>]]    answers after <ms> milliseconds

``GET /__requests`` lists the headers and path of what arrived (so a test can see what the
gateway forwarded); ``DELETE /__requests`` clears it.

    POST /v1/chat/completions   OpenAI chat, plain or streamed, usage on the last chunk
    POST /v1/embeddings         OpenAI embeddings, 8 dimensions, deterministic
    POST /v2/rerank             Cohere v2 rerank, word-overlap scores
    POST /tokenize              vLLM style: ``count`` is the number of words
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
from collections.abc import AsyncIterator
from typing import Any

from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse, StreamingResponse

app = FastAPI(title="model-stub")
SEEN: list[dict[str, Any]] = []
DIM = 8
WORDS = ("fake", "answer", "from", "the", "stub")


def _vec(text: str) -> list[float]:
    return [round(b / 255, 6) for b in hashlib.sha256(text.encode()).digest()[:DIM]]


def _words(text: str) -> set[str]:
    return set(re.findall(r"\w+", text.lower()))


async def _record(request: Request, body: dict[str, Any]) -> None:
    SEEN.append(
        {
            "path": request.url.path,
            "model": body.get("model"),
            "headers": {k.lower(): v for k, v in request.headers.items()},
        }
    )
    del SEEN[:-200]


async def _trigger(text: str) -> Response | None:
    if m := re.search(r"\[\[slow:(\d+)\]\]", text):
        await asyncio.sleep(int(m.group(1)) / 1000)
    if "[[429]]" in text:
        return JSONResponse(
            {"error": {"code": "rate_limit_exceeded", "message": "over the allowance"}},
            status_code=429,
            headers={"retry-after": "7"},
        )
    if "[[residency]]" in text:
        return JSONResponse(
            {"error": {"code": "residency_capacity", "message": "no in-region capacity"}},
            status_code=503,
        )
    if "[[malformed]]" in text:
        return JSONResponse({"unexpected": "shape"})
    return None


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/__requests")
async def requests_seen() -> list[dict[str, Any]]:
    return SEEN


@app.delete("/__requests")
async def clear() -> dict[str, int]:
    SEEN.clear()
    return {"cleared": 1}


@app.post("/v1/chat/completions", response_model=None)
async def chat(request: Request) -> Response:
    body = await request.json()
    await _record(request, body)
    messages = body.get("messages") or []
    text = " ".join(str(m.get("content", "")) for m in messages)
    if refused := await _trigger(text):
        return refused
    model = str(body.get("model", ""))
    usage = {
        "prompt_tokens": len(text.split()),
        "completion_tokens": len(WORDS),
        "total_tokens": len(text.split()) + len(WORDS),
    }
    if not body.get("stream"):
        as_json = (body.get("response_format") or {}).get("type") == "json_object"
        content = json.dumps({"answer": "fake"}) if as_json else " ".join(WORDS)
        return JSONResponse(
            {
                "id": "fake-1",
                "object": "chat.completion",
                "model": model,
                "choices": [{"index": 0, "message": {"role": "assistant", "content": content}}],
                "usage": usage,
            }
        )

    async def frames() -> AsyncIterator[str]:
        for word in WORDS:
            chunk = {"model": model, "choices": [{"index": 0, "delta": {"content": word + " "}}]}
            yield f"data: {json.dumps(chunk)}\n\n"
        if (body.get("stream_options") or {}).get("include_usage"):
            yield f"data: {json.dumps({'model': model, 'choices': [], 'usage': usage})}\n\n"
        yield "data: [DONE]\n\n"

    return StreamingResponse(
        frames(), media_type="text/event-stream", headers={"x-serving-tier": "fake"}
    )


@app.post("/v1/embeddings", response_model=None)
async def embeddings(request: Request) -> Response:
    body = await request.json()
    await _record(request, body)
    inputs = body.get("input")
    texts = [inputs] if isinstance(inputs, str) else list(inputs or [])
    if refused := await _trigger(" ".join(map(str, texts))):
        return refused
    words = sum(len(str(t).split()) for t in texts)
    return JSONResponse(
        {
            "object": "list",
            "model": body.get("model", ""),
            "data": [
                {"object": "embedding", "index": i, "embedding": _vec(str(t))}
                for i, t in enumerate(texts)
            ],
            "usage": {"prompt_tokens": words, "total_tokens": words},
        }
    )


@app.post("/v2/rerank", response_model=None)
async def rerank(request: Request) -> Response:
    body = await request.json()
    await _record(request, body)
    query = str(body.get("query", ""))
    if refused := await _trigger(query):
        return refused
    q = _words(query)
    scored = []
    for i, doc in enumerate(body.get("documents") or []):
        d = _words(str(doc))
        scored.append({"index": i, "relevance_score": round(len(q & d) / (len(q | d) or 1), 6)})
    scored.sort(key=lambda r: -r["relevance_score"])
    top_n = body.get("top_n")
    return JSONResponse(
        {
            "id": "fake-rerank-1",
            "results": scored[:top_n] if top_n else scored,
            "meta": {"billed_units": {"search_units": 1}},
        }
    )


@app.post("/tokenize", response_model=None)
async def tokenize(request: Request) -> Response:
    body = await request.json()
    await _record(request, body)
    prompt = str(body.get("prompt", ""))
    tokens = list(range(len(prompt.split())))
    return JSONResponse({"count": len(tokens), "max_model_len": 8192, "tokens": tokens})
