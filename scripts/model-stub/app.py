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


# ---- the console: try the seven task classes through the real gateway ------------------------
# GET /console is a page; POST /console/call sends one request THROUGH THE GATEWAY (GATEWAY_URL)
# and returns what was sent and what came back. It lives here so the browser stays on one origin.

import os  # noqa: E402
import time  # noqa: E402
import uuid  # noqa: E402

import httpx  # noqa: E402
from fastapi.responses import HTMLResponse  # noqa: E402

GATEWAY_URL = os.environ.get("GATEWAY_URL", "http://agent-router:1975")
CHAT_TASKS = ("draft", "judge", "extract", "refine-query", "refine-instruction")
SYSTEM = {
    "draft": "Write a grounded answer using only the evidence given.",
    "judge": 'Decide whether each claim is supported. Answer {"supported": bool}.',
    "extract": "Extract the requested fields as JSON.",
    "refine-query": "Rewrite the search query to be clearer. Answer JSON.",
    "refine-instruction": "Classify the reviewer's edit instruction. Answer JSON.",
}
TRIGGERS = {
    "": "",
    "429": " [[429]]",
    "residency": " [[residency]]",
    "malformed": " [[malformed]]",
    "slow": " [[slow:1500]]",
}


def _plan(payload: dict[str, Any]) -> tuple[str, dict[str, Any], bool]:
    """The path and body our client (infrastructure/gateway/client.py) would send for this task."""
    task = payload["task"]
    text = str(payload.get("text", "")) + TRIGGERS.get(str(payload.get("trigger", "")), "")
    if task == "embed":
        return "/v1/embeddings", {"model": "embed", "input": [text]}, False
    if task == "rerank":
        passages = [p for p in str(payload.get("passages", "")).splitlines() if p.strip()]
        return "/cohere/v2/rerank", {"model": "rerank", "query": text, "documents": passages}, False
    if task not in CHAT_TASKS:
        raise ValueError(f"unknown task class {task!r}")
    messages = [{"role": "system", "content": SYSTEM[task]}, {"role": "user", "content": text}]
    if task == "draft":
        body: dict[str, Any] = {
            "model": task,
            "temperature": 0.2,
            "stream": True,
            "stream_options": {"include_usage": True},
            "messages": messages,
        }
        return "/v1/chat/completions", body, True
    body = {
        "model": task,
        "temperature": 0,
        "response_format": {"type": "json_object"},
        "messages": messages,
    }
    return "/v1/chat/completions", body, False


def _headers(payload: dict[str, Any]) -> dict[str, str]:
    h = {"x-tenant-id": str(payload.get("tenant") or uuid.uuid4())}
    for key, name in (
        ("residency", "x-residency-mode"),
        ("plan", "x-plan"),
        ("model", "x-preferred-model"),
    ):
        if payload.get(key):
            h[name] = str(payload[key])
    return h


@app.post("/console/call", response_model=None)
async def console_call(request: Request) -> Response:
    payload = await request.json()
    try:
        path, body, streamed = _plan(payload)
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    headers = _headers(payload)
    sent = {"method": "POST", "url": GATEWAY_URL + path, "headers": headers, "body": body}
    started = time.monotonic()

    if streamed:

        async def sse() -> AsyncIterator[str]:
            yield f"event: meta\ndata: {json.dumps(sent)}\n\n"
            try:
                async with httpx.AsyncClient(timeout=60) as client:
                    async with client.stream("POST", sent["url"], json=body, headers=headers) as r:
                        yield f"event: status\ndata: {json.dumps({'status': r.status_code})}\n\n"
                        async for line in r.aiter_lines():
                            if line:
                                yield f"event: frame\ndata: {json.dumps(line)}\n\n"
            except httpx.HTTPError as exc:
                yield f"event: fail\ndata: {json.dumps(str(exc))}\n\n"
            ms = round((time.monotonic() - started) * 1000)
            yield f"event: done\ndata: {json.dumps({'ms': ms})}\n\n"

        return StreamingResponse(sse(), media_type="text/event-stream")

    try:
        async with httpx.AsyncClient(timeout=60) as client:
            r = await client.post(sent["url"], json=body, headers=headers)
    except httpx.HTTPError as exc:
        return JSONResponse({"sent": sent, "fail": f"{type(exc).__name__}: {exc}"})
    try:
        answer: Any = r.json()
    except ValueError:
        answer = r.text
    return JSONResponse(
        {
            "sent": sent,
            "status": r.status_code,
            "ms": round((time.monotonic() - started) * 1000),
            "response_headers": {
                k: v for k, v in r.headers.items() if k in ("retry-after", "content-type")
            },
            "answer": answer,
        }
    )


PAGE = """<!doctype html><html lang="en"><head><meta charset="utf-8"><title>Gateway console</title>
<meta name="viewport" content="width=device-width,initial-scale=1"><style>
:root{--bg:#0f1115;--card:#171a21;--line:#2a2f3a;--tx:#e6e8ee;--mut:#8b93a7;--ac:#8426b0;--ok:#3ecf8e;--bad:#ff6b6b}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--tx);font:14px/1.5 system-ui,sans-serif}
header{padding:18px 24px;border-bottom:1px solid var(--line)}h1{margin:0;font-size:18px}
header p{margin:4px 0 0;color:var(--mut)}main{max-width:1100px;margin:0 auto;padding:20px 24px 60px}
.bar{display:flex;gap:12px;flex-wrap:wrap;background:var(--card);border:1px solid var(--line);border-radius:10px;padding:12px 14px;margin-bottom:18px}
.bar label{display:flex;flex-direction:column;font-size:12px;color:var(--mut);gap:4px}
input,textarea,select{background:#0f1115;color:var(--tx);border:1px solid var(--line);border-radius:6px;padding:7px 9px;font:inherit}
textarea{width:100%;min-height:58px;resize:vertical}.card{background:var(--card);border:1px solid var(--line);border-radius:10px;margin-bottom:14px}
.card h2{margin:0;padding:12px 14px;font-size:15px;display:flex;gap:10px;align-items:baseline;border-bottom:1px solid var(--line)}
.card h2 small{color:var(--mut);font-weight:400}.body{padding:12px 14px;display:grid;gap:10px}
.row{display:flex;gap:10px;align-items:center;flex-wrap:wrap}button{background:var(--ac);color:#fff;border:0;border-radius:6px;padding:8px 14px;font:inherit;cursor:pointer}
button:disabled{opacity:.5}pre{margin:0;background:#0f1115;border:1px solid var(--line);border-radius:6px;padding:10px;overflow:auto;max-height:260px;font:12px/1.5 ui-monospace,monospace;white-space:pre-wrap;word-break:break-word}
.out{display:none;gap:8px}.tag{font-size:12px;border-radius:999px;padding:2px 9px;border:1px solid var(--line);color:var(--mut)}
.ok{color:var(--ok);border-color:var(--ok)}.bad{color:var(--bad);border-color:var(--bad)}h3{margin:6px 0 0;font-size:12px;color:var(--mut);font-weight:600;text-transform:uppercase;letter-spacing:.05em}
a{color:#c58be6}
</style></head><body><header><h1>Gateway console</h1>
<p>Each card sends the request our client would send, <b>through the real gateway</b> (Agent Router) to the <b>model stub</b>, a stand-in for the AI models. Answers are canned, not model output.</p></header><main>
<div class="bar">
<label>x-tenant-id<input id="tenant" size="38"></label>
<label>x-plan<input id="plan" value="pro" size="8"></label>
<label>x-residency-mode<select id="residency"><option>standard</option><option>strict</option></select></label>
<label>x-preferred-model<input id="model" placeholder="(none)" size="12"></label>
<label>make it fail<select id="trigger"><option value="">no, answer normally</option>
<option value="429">rate limit (429)</option><option value="residency">no in-region capacity (503)</option>
<option value="malformed">malformed answer</option><option value="slow">slow (1.5 s)</option></select></label>
<label>links<span><a href="http://localhost:1064/health" target="_blank">gateway health</a> ·
<a href="http://localhost:1064/metrics" target="_blank">gateway metrics</a></span></label></div>
<div id="cards"></div></main><script>
const TASKS=[
{t:"embed",path:"/v1/embeddings",schema:"OpenAI",use:"Turns a question into numbers for the vector search",text:"What are the payment terms?"},
{t:"rerank",path:"/cohere/v2/rerank",schema:"Cohere v2",use:"Re-orders the first-stage passages by how well they answer",text:"payment terms",passages:"the payment terms are net 30 days\\nweather in Dubai\\nterms of delivery and acceptance"},
{t:"draft",path:"/v1/chat/completions (streamed)",schema:"OpenAI",use:"Writes the answer or section text",text:"Summarise the payment terms."},
{t:"judge",path:"/v1/chat/completions",schema:"OpenAI",use:"Checks whether claims are supported by the evidence",text:"Claim: payment is due in 30 days."},
{t:"extract",path:"/v1/chat/completions",schema:"OpenAI",use:"Pulls structured fields out of documents",text:"Extract the contract value and currency."},
{t:"refine-query",path:"/v1/chat/completions",schema:"OpenAI",use:"Rewrites a search query to be clearer",text:"pay terms??"},
{t:"refine-instruction",path:"/v1/chat/completions",schema:"OpenAI",use:"Interprets a reviewer's edit instruction",text:"make this section shorter"}];
const $=id=>document.getElementById(id);$("tenant").value=crypto.randomUUID();
const j=o=>JSON.stringify(o,null,2);
$("cards").innerHTML=TASKS.map(k=>`<div class="card" id="c-${k.t}"><h2>${k.t} <small>${k.use}</small></h2><div class="body">
<div class="row"><span class="tag">POST ${k.path}</span><span class="tag">backend schema: ${k.schema}</span></div>
<textarea id="text-${k.t}">${k.text}</textarea>
${k.passages?`<h3>passages (one per line)</h3><textarea id="pass-${k.t}">${k.passages}</textarea>`:""}
<div class="row"><button id="go-${k.t}">Send through the gateway</button><span id="st-${k.t}"></span></div>
<div class="out" id="out-${k.t}"><h3>sent</h3><pre id="sent-${k.t}"></pre><h3>answer</h3><pre id="ans-${k.t}"></pre>
<h3>what the backend received</h3><pre id="seen-${k.t}"></pre></div></div></div>`).join("");
async function seen(sent){try{const r=await (await fetch("/__requests")).json();
const m=r.filter(x=>x.model===sent.body.model).pop();
return m?j({path_at_backend:m.path,"x-ai-eg-model":m.headers["x-ai-eg-model"],"x-tenant-id":m.headers["x-tenant-id"],
"x-plan":m.headers["x-plan"],"x-residency-mode":m.headers["x-residency-mode"],"x-preferred-model":m.headers["x-preferred-model"]}):"(nothing recorded)";}catch(e){return String(e)}}
function pill(el,ok,txt){el.innerHTML=`<span class="tag ${ok?"ok":"bad"}">${txt}</span>`}
async function go(k){const t=k.t,btn=$("go-"+t);btn.disabled=true;$("out-"+t).style.display="grid";
$("sent-"+t).textContent="…";$("ans-"+t).textContent="";$("seen-"+t).textContent="";pill($("st-"+t),true,"sending…");
const payload={task:t,text:$("text-"+t).value,passages:$("pass-"+t)?.value,trigger:$("trigger").value,tenant:$("tenant").value,
plan:$("plan").value,residency:$("residency").value,model:$("model").value};
try{const res=await fetch("/console/call",{method:"POST",headers:{"content-type":"application/json"},body:JSON.stringify(payload)});
if(t==="draft"){const rd=res.body.getReader(),dec=new TextDecoder();let buf="",text="",usage=null,sent=null;
while(true){const {done,value}=await rd.read();if(done)break;buf+=dec.decode(value,{stream:true});let i;
while((i=buf.indexOf("\\n\\n"))>=0){const blk=buf.slice(0,i);buf=buf.slice(i+2);
const ev=(blk.match(/^event: (.*)$/m)||[])[1],data=JSON.parse((blk.match(/^data: (.*)$/m)||[])[1]||"null");
if(ev==="meta"){sent=data;$("sent-"+t).textContent=j(data)}
else if(ev==="status")pill($("st-"+t),data.status<400,"HTTP "+data.status);
else if(ev==="frame"&&data.startsWith("data: ")&&data!=="data: [DONE]"){try{const o=JSON.parse(data.slice(6));
const d=o.choices?.[0]?.delta?.content;if(d)text+=d;if(o.usage)usage=o.usage}catch{text+="\\n"+data}
$("ans-"+t).textContent=text+(usage?"\\n\\nusage: "+JSON.stringify(usage):"")}
else if(ev==="frame"&&!data.startsWith("data: "))$("ans-"+t).textContent+="\\n"+data;
else if(ev==="fail")pill($("st-"+t),false,data);
else if(ev==="done")$("st-"+t).insertAdjacentHTML("beforeend",` <span class="tag">${data.ms} ms</span>`)}}
if(sent)$("seen-"+t).textContent=await seen(sent)}
else{const o=await res.json();$("sent-"+t).textContent=j(o.sent||{});
if(o.fail){pill($("st-"+t),false,o.fail);$("ans-"+t).textContent=o.fail}
else{pill($("st-"+t),o.status<400,"HTTP "+o.status);$("st-"+t).insertAdjacentHTML("beforeend",` <span class="tag">${o.ms} ms</span>`);
$("ans-"+t).textContent=(o.response_headers["retry-after"]?"retry-after: "+o.response_headers["retry-after"]+"\\n":"")+(typeof o.answer==="string"?o.answer:j(o.answer));
$("seen-"+t).textContent=await seen(o.sent)}}}
catch(e){pill($("st-"+t),false,String(e))}btn.disabled=false}
TASKS.forEach(k=>$("go-"+k.t).onclick=()=>go(k));
</script></body></html>"""


@app.get("/console", response_class=HTMLResponse)
async def console_page() -> str:
    return PAGE
