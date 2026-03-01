import json
import os
from typing import Any, Dict, Iterable, Optional

import httpx
from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse, StreamingResponse

app = FastAPI()

UPSTREAM_BASE = os.environ.get("UPSTREAM_BASE", "https://llm-proxy.imla.hs-offenburg.de").rstrip("/")
UPSTREAM_TIMEOUT = float(os.environ.get("UPSTREAM_TIMEOUT", "120"))

# Parameter, die Jan/litellm gern mitschickt, die OpenAI-ChatCompletions aber nicht kennt
DROP_KEYS = {
    "top_k",
    "repeat_penalty",
    "presence_penalty",  # (falls upstream das nicht mag, sonst rausnehmen)
    "frequency_penalty", # (falls upstream das nicht mag, sonst rausnehmen)
    "seed",              # (dito)
}

def _auth_header(req: Request) -> Dict[str, str]:
    # Jan sendet normalerweise Authorization: Bearer <key>.
    # Falls nicht, kannst du per ENV UPSTREAM_BEARER ein Default setzen.
    auth = req.headers.get("authorization")
    if not auth:
        bearer = os.environ.get("UPSTREAM_BEARER")
        if bearer:
            auth = f"Bearer {bearer}"
    return {"Authorization": auth} if auth else {}

def _copy_headers(req: Request) -> Dict[str, str]:
    hdrs = {"Content-Type": "application/json"}
    hdrs.update(_auth_header(req))
    return hdrs

def _sanitize_payload(payload: Dict[str, Any]) -> Dict[str, Any]:
    # drop unknown keys
    for k in list(payload.keys()):
        if k in DROP_KEYS:
            payload.pop(k, None)

    # Jan/litellm setzt häufig stream=true; wir schalten upstream immer auf false (Stabilität).
    payload["stream"] = False
    return payload

async def _upstream_post(path: str, req: Request, payload: Dict[str, Any]) -> httpx.Response:
    url = f"{UPSTREAM_BASE}{path}"
    headers = _copy_headers(req)
    async with httpx.AsyncClient(timeout=UPSTREAM_TIMEOUT) as client:
        r = await client.post(url, headers=headers, json=payload)
        return r

@app.get("/v1/models")
@app.get("/v1//models")
@app.get("/models")
@app.get("/v/models")
async def models(req: Request):
    r = await _upstream_post("/v1/models", req, {})
    return Response(content=r.content, status_code=r.status_code, media_type=r.headers.get("content-type", "application/json"))

@app.post("/v1/chat/completions")
async def chat_completions(req: Request):
    payload = await req.json()
    payload = _sanitize_payload(payload)
    r = await _upstream_post("/v1/chat/completions", req, payload)
    return Response(content=r.content, status_code=r.status_code, media_type=r.headers.get("content-type", "application/json"))

def _chat_to_responses(chat: Dict[str, Any], model: str) -> Dict[str, Any]:
    # Minimal "Responses API"-ähnliches Objekt, das viele Clients akzeptieren.
    text = ""
    try:
        text = chat["choices"][0]["message"]["content"] or ""
    except Exception:
        text = ""

    return {
        "id": chat.get("id", "resp_compat"),
        "object": "response",
        "model": chat.get("model", model),
        "output": [
            {
                "type": "message",
                "role": "assistant",
                "content": [{"type": "output_text", "text": text}],
            }
        ],
        "usage": chat.get("usage", {}),
    }

def _sse_event(obj: Dict[str, Any]) -> bytes:
    return f"data: {json.dumps(obj, ensure_ascii=False)}\n\n".encode("utf-8")

@app.post("/v1/responses")
async def responses_compat(req: Request):
    """
    Jan schickt /v1/responses. Dein HS-Gateway kann das nicht (404),
    daher mappen wir auf /v1/chat/completions.
    """
    payload = await req.json()

    # "input" (Responses) -> "messages" (ChatCompletions)
    model = payload.get("model", "gpt-4.1-mini")

    messages = payload.get("messages")
    if messages is None:
        # input kann string oder array sein
        inp = payload.get("input", "")
        if isinstance(inp, str):
            messages = [{"role": "user", "content": inp}]
        elif isinstance(inp, list):
            # best effort: falls Jan schon message-objekte liefert
            messages = inp
        else:
            messages = [{"role": "user", "content": str(inp)}]

    chat_payload: Dict[str, Any] = {
        "model": model,
        "messages": messages,
        # optional: temperature/top_p etc. wenn vorhanden
    }

    # copy whitelisted sampling params
    for k in ("temperature", "top_p", "max_tokens", "max_completion_tokens"):
        if k in payload:
            chat_payload[k] = payload[k]

    chat_payload = _sanitize_payload(chat_payload)

    upstream = await _upstream_post("/v1/chat/completions", req, chat_payload)

    # wenn upstream Fehler liefert: direkt durchreichen
    if upstream.status_code >= 400:
        ct = upstream.headers.get("content-type", "application/json")
        return Response(content=upstream.content, status_code=upstream.status_code, media_type=ct)

    # upstream kann gzip/chunked etc. sein – hier sicher als JSON lesen
    try:
        chat_json = upstream.json()
    except Exception:
        # Fallback: Text zurückgeben
        return Response(content=upstream.content, status_code=502, media_type="text/plain")

    resp_obj = _chat_to_responses(chat_json, model=model)

    # Wenn Jan "stream": true wollte, geben wir ein Mini-SSE (1 Event + DONE).
    wants_stream = bool(payload.get("stream", False))
    if wants_stream:
        async def gen() -> Iterable[bytes]:
            yield _sse_event(resp_obj)
            yield b"data: [DONE]\n\n"

        return StreamingResponse(gen(), media_type="text/event-stream")

    return JSONResponse(resp_obj)

@app.post("/v1/embeddings")
async def embeddings(req: Request):
    # Embeddings können wir 1:1 durchreichen (Jan braucht das für "Add documents", sofern UI es nutzt)
    payload = await req.json()
    # drop evtl. unbekannte keys, aber embeddings ist meist sauber
    r = await _upstream_post("/v1/embeddings", req, payload)
    return Response(content=r.content, status_code=r.status_code, media_type=r.headers.get("content-type", "application/json"))
