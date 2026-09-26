"""A scriptable fake OpenAI-compatible model server — TEST DOUBLE ONLY.

It lets tests exercise every boundary deterministically: normal answers, schema-valid
JSON, streaming, embeddings, and failure modes (empty content, invalid JSON, HTTP
errors, slow responses, unknown model names).

Behaviour is controlled through ``POST /_control`` (mode) and ``POST /_control/rules``
(substring → canned reply). It is never imported by application code.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
import threading
import time
from typing import Any

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, PlainTextResponse, StreamingResponse

MODEL_NAMES = ["fake-chat", "fake-embed"]


class State:
    def __init__(self) -> None:
        self.mode = "normal"  # normal|empty|invalid_json|http_500|http_401|slow|reasoning_only
        self.rules: list[tuple[str, str]] = []
        self.calls: list[dict[str, Any]] = []
        self.dimensions = 64


state = State()
app = FastAPI()


def _instance(schema: dict[str, Any], defs: dict[str, Any] | None = None) -> Any:
    """Generate a minimal instance that validates against a JSON schema."""
    defs = defs if defs is not None else schema.get("$defs", {})
    if "$ref" in schema:
        return _instance(defs[schema["$ref"].split("/")[-1]], defs)
    if "anyOf" in schema:
        non_null = [s for s in schema["anyOf"] if s.get("type") != "null"]
        return _instance(non_null[0], defs) if non_null else None
    if "enum" in schema:
        return schema["enum"][0]
    if "default" in schema and schema.get("type") != "object":
        return schema["default"]
    kind = schema.get("type")
    if kind == "object" or "properties" in schema:
        props = schema.get("properties", {})
        return {k: _instance(v, defs) for k, v in props.items() if k in schema.get("required", list(props))}
    if kind == "array":
        return []
    if kind == "integer":
        return 1
    if kind == "number":
        return 1.0
    if kind == "boolean":
        return True
    return "The fake model answered this field."


def _text_of(messages: list[dict[str, Any]]) -> str:
    return "\n".join(str(m.get("content") or "") for m in messages)


def _reply(payload: dict[str, Any]) -> str:
    text = _text_of(payload.get("messages", []))
    for needle, reply in state.rules:
        if needle in text:
            return reply
    fmt = payload.get("response_format") or {}
    if fmt.get("type") == "json_schema":
        return json.dumps(_instance(fmt["json_schema"]["schema"]))
    if fmt.get("type") == "json_object":
        return json.dumps({"intent": "explain", "complexity": "simple"})
    if "Reply with exactly" in text or "PONG" in text:
        return "PONG OK"
    return "This is a deterministic answer from the fake model [S1]."


@app.post("/_control")
async def control(request: Request) -> dict[str, Any]:
    body = await request.json()
    state.mode = body.get("mode", state.mode)
    if body.get("reset"):
        state.rules.clear()
        state.calls.clear()
        state.mode = "normal"
    return {"mode": state.mode}


@app.post("/_control/rules")
async def rules(request: Request) -> dict[str, Any]:
    body = await request.json()
    state.rules = [(r["contains"], r["reply"]) for r in body.get("rules", [])]
    return {"rules": len(state.rules)}


@app.get("/_control/calls")
async def calls() -> list[dict[str, Any]]:
    return state.calls


@app.get("/v1/models")
async def models() -> Any:
    if state.mode == "http_401":
        return JSONResponse({"error": {"message": "invalid api key"}}, status_code=401)
    return {"object": "list", "data": [{"id": n, "object": "model", "max_model_len": 32768} for n in MODEL_NAMES]}


@app.post("/v1/chat/completions")
async def chat(request: Request) -> Any:
    payload = await request.json()
    state.calls.append({"endpoint": "chat", "model": payload.get("model"), "stream": payload.get("stream"),
                        "has_schema": bool(payload.get("response_format"))})
    if payload.get("model") not in MODEL_NAMES:
        return JSONResponse({"error": {"message": f"The model `{payload.get('model')}` does not exist."}}, status_code=404)
    if state.mode == "http_500":
        return JSONResponse({"error": {"message": "internal error"}}, status_code=500)
    if state.mode == "http_401":
        return JSONResponse({"error": {"message": "invalid api key"}}, status_code=401)
    if state.mode == "invalid_json":
        return PlainTextResponse("this is not json", status_code=200)
    if state.mode == "slow":
        await asyncio.sleep(5)
    content = _reply(payload)
    message: dict[str, Any] = {"role": "assistant", "content": content}
    if state.mode == "empty":
        message["content"] = ""
    elif state.mode == "reasoning_only":
        message["content"] = ""
        message["reasoning_content"] = "thinking about it..."
    if payload.get("stream"):
        async def gen():
            text = message["content"] or ""
            for i in range(0, len(text), 12):
                chunk = {"choices": [{"index": 0, "delta": {"content": text[i:i + 12]}, "finish_reason": None}]}
                yield f"data: {json.dumps(chunk)}\n\n"
            yield f"data: {json.dumps({'choices': [{'index': 0, 'delta': {}, 'finish_reason': 'stop'}]})}\n\n"
            yield f"data: {json.dumps({'choices': [], 'usage': {'prompt_tokens': 10, 'completion_tokens': 5}})}\n\n"
            yield "data: [DONE]\n\n"

        return StreamingResponse(gen(), media_type="text/event-stream")
    return {
        "id": "chatcmpl-fake", "object": "chat.completion", "created": int(time.time()), "model": payload["model"],
        "choices": [{"index": 0, "message": message, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 10, "completion_tokens": max(1, len(content) // 4)},
    }


def _vector(text: str) -> list[float]:
    """Deterministic bag-of-words embedding so similar texts are close."""
    vec = [0.0] * state.dimensions
    for word in text.lower().split():
        h = int(hashlib.sha256(word.encode()).hexdigest(), 16)
        vec[h % state.dimensions] += 1.0
    norm = math.sqrt(sum(v * v for v in vec)) or 1.0
    return [v / norm for v in vec]


@app.post("/v1/embeddings")
async def embeddings(request: Request) -> Any:
    payload = await request.json()
    inputs = payload["input"] if isinstance(payload["input"], list) else [payload["input"]]
    state.calls.append({"endpoint": "embeddings", "count": len(inputs)})
    if state.mode == "http_500":
        return JSONResponse({"error": {"message": "internal error"}}, status_code=500)
    return {"object": "list", "data": [{"index": i, "embedding": _vector(t)} for i, t in enumerate(inputs)],
            "usage": {"prompt_tokens": 5}}


class FakeModelServer:
    """Runs the fake server in a background thread on a free port."""

    def __init__(self, port: int) -> None:
        self.port = port
        self.url = f"http://127.0.0.1:{port}"
        self._server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
        self._thread = threading.Thread(target=self._server.run, daemon=True)

    def start(self) -> None:
        self._thread.start()
        deadline = time.time() + 10
        while not self._server.started and time.time() < deadline:
            time.sleep(0.05)

    def stop(self) -> None:
        self._server.should_exit = True
        self._thread.join(timeout=5)
