"""Ollama provider using the native API (/api/chat, /api/tags, /api/embed)."""

from __future__ import annotations

import json
import time
import uuid
from collections.abc import AsyncIterator
from typing import Any

import httpx

from app.core.exceptions import ErrorCode, ModelError
from app.models.providers.base import (
    ChatChunk,
    ChatMessage,
    ChatRequest,
    ChatResponse,
    EmbedResponse,
    ThinkStreamFilter,
    ToolCall,
    Usage,
    strip_reasoning,
)
from app.models.providers.openai_compatible import OpenAICompatibleProvider, _truncate


class OllamaProvider(OpenAICompatibleProvider):
    """Reuses error mapping/validation from the OpenAI-compatible provider."""

    @property
    def base_url(self) -> str:
        base = self.config.endpoint.rstrip("/")
        return base[: -len("/v1")] if base.endswith("/v1") else base

    def _map_http_error(self, response: httpx.Response) -> ModelError:
        body = (response.text or "").lower()
        if response.status_code == 404 and "model" in body and ("not found" in body or "pull" in body):
            return self.error(
                ErrorCode.MODEL_WRONG_NAME,
                f"Ollama does not have a model named '{self.config.model}'.",
                detail=_truncate(response.text),
                hint=f"Run: ollama pull {self.config.model}  (or fix the model name).",
            )
        return super()._map_http_error(response)

    def _serialize(self, messages: list[ChatMessage]) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for m in messages:
            content = m.content if isinstance(m.content, str) else json.dumps(m.content) if m.content else ""
            item: dict[str, Any] = {"role": m.role, "content": content}
            if m.tool_calls:
                item["tool_calls"] = [{"function": {"name": tc.name, "arguments": tc.arguments}} for tc in m.tool_calls]
            if m.role == "tool" and m.name:
                item["tool_name"] = m.name
            out.append(item)
        return out

    def _payload(self, request: ChatRequest, *, stream: bool) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model": self.config.model,
            "messages": self._serialize(request.messages),
            "stream": stream,
            "options": {
                "temperature": request.temperature if request.temperature is not None else self.config.temperature,
                "num_predict": request.max_tokens or self.config.max_output_tokens,
                "num_ctx": self.config.context_length,
            },
        }
        if request.stop:
            payload["options"]["stop"] = request.stop
        if request.tools and self.config.capabilities.supports_tools:
            payload["tools"] = [
                {"type": "function", "function": {"name": t.name, "description": t.description, "parameters": t.parameters}}
                for t in request.tools
            ]
        elif request.response_schema is not None and self.config.capabilities.supports_json_schema:
            payload["format"] = request.response_schema
        elif request.json_mode or request.response_schema is not None:
            payload["format"] = "json"
        payload.update(self.config.extra_body)
        return payload

    async def list_models(self) -> list[str]:
        url = f"{self.base_url}/api/tags"
        try:
            response = await self.http.get(url, headers=self._headers(), timeout=15)
        except httpx.HTTPError as exc:
            raise self._map_transport_error(exc, url) from exc
        if response.status_code >= 400:
            raise self._map_http_error(response)
        try:
            return [str(m["name"]) for m in response.json().get("models", [])]
        except (ValueError, KeyError, TypeError, AttributeError) as exc:
            raise self.error(ErrorCode.MODEL_INVALID_RESPONSE, "GET /api/tags returned an unexpected response.",
                             detail=_truncate(response.text)) from exc

    async def chat(self, request: ChatRequest) -> ChatResponse:
        started = time.perf_counter()
        data = await self._post_json("/api/chat", self._payload(request, stream=False))
        latency_ms = int((time.perf_counter() - started) * 1000)
        message = data.get("message")
        if not isinstance(message, dict):
            raise self.error(ErrorCode.MODEL_INVALID_RESPONSE, "Ollama response has no message.",
                             detail=_truncate(json.dumps(data)))
        content, inline_reasoning = strip_reasoning(message.get("content") or "")
        tool_calls = [
            ToolCall(id=uuid.uuid4().hex[:12], name=tc["function"]["name"],
                     arguments=tc["function"].get("arguments") or {},
                     raw_arguments=json.dumps(tc["function"].get("arguments") or {}))
            for tc in message.get("tool_calls") or []
            if isinstance(tc, dict) and (tc.get("function") or {}).get("name")
        ]
        had_reasoning = inline_reasoning or bool(message.get("thinking"))
        if not content.strip() and not tool_calls:
            raise self.error(
                ErrorCode.MODEL_EMPTY_RESPONSE,
                f"The model '{self.config.id}' returned an empty response (HTTP 200 with no content).",
                detail=f"done_reason={data.get('done_reason')}",
                hint="The model produced only reasoning tokens." if had_reasoning else None,
            )
        return ChatResponse(
            model_id=self.config.id,
            model_name=str(data.get("model") or self.config.model),
            content=content,
            tool_calls=tool_calls,
            finish_reason=data.get("done_reason"),
            usage=Usage(prompt_tokens=int(data.get("prompt_eval_count") or 0),
                        completion_tokens=int(data.get("eval_count") or 0)),
            latency_ms=latency_ms,
            had_reasoning=had_reasoning,
        )

    async def stream_chat(self, request: ChatRequest) -> AsyncIterator[ChatChunk]:
        url = f"{self.base_url}/api/chat"
        think = ThinkStreamFilter()
        produced = False
        finished = False
        try:
            async with self.http.stream("POST", url, json=self._payload(request, stream=True), headers=self._headers(),
                                        timeout=self.config.timeout_s) as response:
                if response.status_code >= 400:
                    await response.aread()
                    raise self._map_http_error(response)
                async for line in response.aiter_lines():
                    if not line.strip():
                        continue
                    try:
                        data = json.loads(line)
                    except json.JSONDecodeError as exc:
                        raise self.error(ErrorCode.MODEL_STREAM_ERROR, "The model stream contained malformed data.",
                                         detail=_truncate(line)) from exc
                    if data.get("error"):
                        raise self.error(ErrorCode.MODEL_STREAM_ERROR, "Ollama reported an error during streaming.",
                                         detail=_truncate(str(data["error"])))
                    text = think.feed((data.get("message") or {}).get("content") or "")
                    if text:
                        produced = produced or bool(text.strip())
                        yield ChatChunk(content=text)
                    if data.get("done"):
                        finished = True
                        yield ChatChunk(
                            finish_reason=data.get("done_reason") or "stop",
                            usage=Usage(prompt_tokens=int(data.get("prompt_eval_count") or 0),
                                        completion_tokens=int(data.get("eval_count") or 0)),
                        )
                        break
        except httpx.HTTPError as exc:
            raise self._map_transport_error(exc, url) from exc
        tail = think.flush()
        if tail:
            produced = produced or bool(tail.strip())
            yield ChatChunk(content=tail)
        if not produced:
            raise self.error(ErrorCode.MODEL_EMPTY_RESPONSE, f"The model '{self.config.id}' streamed no content.")
        if not finished:
            raise self.error(ErrorCode.MODEL_STREAM_ERROR, "The model stream ended unexpectedly before completion.")

    async def embed(self, texts: list[str]) -> EmbedResponse:
        if not texts:
            return EmbedResponse(model_id=self.config.id, vectors=[], dimensions=self.config.embedding_dimensions or 0)
        started = time.perf_counter()
        data = await self._post_json("/api/embed", {"model": self.config.model, "input": texts})
        vectors = data.get("embeddings")
        if not isinstance(vectors, list) or len(vectors) != len(texts):
            raise self.error(ErrorCode.MODEL_INVALID_RESPONSE, "Ollama embedding response has the wrong number of vectors.")
        return self._validated_embeddings([list(map(float, v)) for v in vectors], started,
                                          {"prompt_tokens": data.get("prompt_eval_count") or 0})
