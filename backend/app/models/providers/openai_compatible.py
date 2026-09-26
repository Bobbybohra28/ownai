"""Provider for OpenAI-compatible HTTP APIs (vLLM, llama.cpp server, LM Studio, TGI, Ollama /v1…).

Every boundary is validated: transport errors, HTTP status, JSON shape, and the
presence of actual content. HTTP 200 with empty content is a failure
(``MODEL_EMPTY_RESPONSE``), never a success.
"""

from __future__ import annotations

import json
import re
import time
import uuid
from collections.abc import AsyncIterator
from typing import Any

import httpx

from app.core.exceptions import ErrorCode, ModelError
from app.core.logging import get_logger
from app.models.providers.base import (
    ChatChunk,
    ChatMessage,
    ChatRequest,
    ChatResponse,
    EmbedResponse,
    ModelProvider,
    RerankResult,
    ThinkStreamFilter,
    ToolCall,
    Usage,
    strip_reasoning,
)

log = get_logger(__name__)


def _truncate(text: str, limit: int = 500) -> str:
    return text if len(text) <= limit else text[:limit] + "…"


def parse_tool_arguments(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    if raw in (None, ""):
        return {}
    try:
        value = json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return {"__invalid_json__": str(raw)}
    return value if isinstance(value, dict) else {"value": value}


class OpenAICompatibleProvider(ModelProvider):
    # ---- helpers ----------------------------------------------------------------------------
    @property
    def base_url(self) -> str:
        return self.config.endpoint.rstrip("/")

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json", **self.config.headers}
        if self.config.api_key and self.config.api_key.get_secret_value():
            headers["Authorization"] = f"Bearer {self.config.api_key.get_secret_value()}"
        return headers

    def _map_transport_error(self, exc: Exception, url: str) -> ModelError:
        if isinstance(exc, httpx.TimeoutException):
            return self.error(
                ErrorCode.MODEL_TIMEOUT,
                f"The model '{self.config.id}' did not respond within {self.config.timeout_s:.0f}s.",
                detail=f"{type(exc).__name__} for {url}",
                hint="The model server may be overloaded or the request too large. Increase timeout_s or reduce context.",
            )
        if isinstance(exc, httpx.ConnectError | httpx.RemoteProtocolError | httpx.ReadError | httpx.WriteError):
            return self.error(
                ErrorCode.MODEL_CONNECTION_ERROR,
                f"Could not connect to the model server for '{self.config.id}' at {self.base_url}.",
                detail=f"{type(exc).__name__}: {exc}",
                hint="Check that the model server (vLLM/Ollama) is running and the endpoint URL is correct.",
            )
        return self.error(ErrorCode.MODEL_CONNECTION_ERROR, f"Request to model '{self.config.id}' failed.", detail=repr(exc))

    def _map_http_error(self, response: httpx.Response) -> ModelError:
        status = response.status_code
        body = _truncate(response.text or "")
        lowered = body.lower()
        if status in (401, 403):
            return self.error(
                ErrorCode.MODEL_AUTH_ERROR,
                f"The model server rejected the credentials for '{self.config.id}' (HTTP {status}).",
                detail=body,
                hint="Check the API key configured for this model.",
            )
        if ("model" in lowered and any(s in lowered for s in ("not found", "does not exist", "not exist", "unknown model"))):
            return self.error(
                ErrorCode.MODEL_WRONG_NAME,
                f"The model server does not serve a model named '{self.config.model}'.",
                detail=body,
                hint="Check the model name in your configuration against GET /v1/models.",
            )
        if status == 404:
            return self.error(
                ErrorCode.MODEL_HTTP_ERROR,
                f"The model endpoint returned 404 for '{self.config.id}'.",
                detail=f"{response.request.url}: {body}",
                hint="The endpoint URL is probably wrong. OpenAI-compatible endpoints usually end with /v1.",
            )
        if status in (400, 413) and any(s in lowered for s in ("context length", "maximum context", "too many tokens",
                                                               "context window", "context size", "exceed_context")):
            server_ctx = re.search(r"n_ctx\\?\"?:\s*(\d+)|context (?:size|length) \((\d+) tokens\)", body)
            actual = next((g for g in server_ctx.groups() if g), None) if server_ctx else None
            hint = "Reduce the request size or use a model with a larger context window."
            if actual and int(actual) < self.config.context_length:
                hint = (f"The server's real context window is {actual} tokens but context_length is configured as "
                        f"{self.config.context_length}. Increase the server's context (vLLM --max-model-len; Ollama: use "
                        f"provider 'ollama' or set OLLAMA_CONTEXT_LENGTH) or lower context_length.")
            return self.error(
                ErrorCode.MODEL_CONTEXT_LENGTH_EXCEEDED,
                f"The request is larger than the context window of '{self.config.id}'.",
                detail=body, hint=hint,
            )
        return self.error(
            ErrorCode.MODEL_HTTP_ERROR,
            f"The model server returned HTTP {status} for '{self.config.id}'.",
            detail=body,
        )

    async def _post_json(self, path: str, payload: dict[str, Any], *, timeout: float | None = None) -> dict[str, Any]:
        url = f"{self.base_url}{path}"
        try:
            response = await self.http.post(url, json=payload, headers=self._headers(), timeout=timeout or self.config.timeout_s)
        except httpx.HTTPError as exc:
            raise self._map_transport_error(exc, url) from exc
        if response.status_code >= 400:
            raise self._map_http_error(response)
        try:
            data = response.json()
        except (json.JSONDecodeError, ValueError) as exc:
            raise self.error(
                ErrorCode.MODEL_INVALID_RESPONSE,
                f"The model server returned a response that is not valid JSON for '{self.config.id}'.",
                detail=_truncate(response.text),
            ) from exc
        if not isinstance(data, dict):
            raise self.error(ErrorCode.MODEL_INVALID_RESPONSE, "The model server returned an unexpected JSON structure.",
                             detail=_truncate(response.text))
        if "error" in data and not data.get("choices") and not data.get("data"):
            err = data["error"]
            message = err.get("message") if isinstance(err, dict) else str(err)
            raise self.error(ErrorCode.MODEL_HTTP_ERROR, f"The model server reported an error: {_truncate(str(message), 200)}",
                             detail=_truncate(json.dumps(data)))
        return data

    def _serialize_messages(self, messages: list[ChatMessage]) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for m in messages:
            item: dict[str, Any] = {"role": m.role, "content": m.content if m.content is not None else ""}
            if m.tool_calls:
                item["tool_calls"] = [
                    {"id": tc.id, "type": "function",
                     "function": {"name": tc.name, "arguments": tc.raw_arguments or json.dumps(tc.arguments)}}
                    for tc in m.tool_calls
                ]
            if m.tool_call_id:
                item["tool_call_id"] = m.tool_call_id
            if m.name and m.role == "tool":
                item["name"] = m.name
            out.append(item)
        return out

    def build_chat_payload(self, request: ChatRequest, *, stream: bool) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model": self.config.model,
            "messages": self._serialize_messages(request.messages),
            "temperature": request.temperature if request.temperature is not None else self.config.temperature,
            "max_tokens": request.max_tokens or self.config.max_output_tokens,
            "stream": stream,
        }
        if stream:
            payload["stream_options"] = {"include_usage": True}
        if request.stop:
            payload["stop"] = request.stop
        if request.tools and self.config.capabilities.supports_tools:
            payload["tools"] = [
                {"type": "function", "function": {"name": t.name, "description": t.description, "parameters": t.parameters}}
                for t in request.tools
            ]
            payload["tool_choice"] = "auto"
        elif request.response_schema is not None and self.config.capabilities.supports_json_schema:
            payload["response_format"] = {
                "type": "json_schema",
                "json_schema": {"name": "output", "schema": request.response_schema},
            }
        elif request.json_mode or request.response_schema is not None:
            payload["response_format"] = {"type": "json_object"}
        payload.update(self.config.extra_body)
        return payload

    # ---- API --------------------------------------------------------------------------------
    async def list_models(self) -> list[str]:
        url = f"{self.base_url}/models"
        try:
            response = await self.http.get(url, headers=self._headers(), timeout=min(self.config.timeout_s, 15))
        except httpx.HTTPError as exc:
            raise self._map_transport_error(exc, url) from exc
        if response.status_code >= 400:
            raise self._map_http_error(response)
        try:
            data = response.json()
            self.server_context_lengths = {str(m["id"]): int(m["max_model_len"]) for m in data.get("data", [])
                                           if isinstance(m, dict) and m.get("max_model_len")}
            return [str(m["id"]) for m in data.get("data", [])]
        except (ValueError, KeyError, TypeError, AttributeError) as exc:
            raise self.error(ErrorCode.MODEL_INVALID_RESPONSE, "GET /models returned an unexpected response.",
                             detail=_truncate(response.text)) from exc

    def parse_chat_response(self, data: dict[str, Any], latency_ms: int) -> ChatResponse:
        choices = data.get("choices")
        if not isinstance(choices, list) or not choices:
            raise self.error(ErrorCode.MODEL_INVALID_RESPONSE,
                             f"The model '{self.config.id}' returned no choices.", detail=_truncate(json.dumps(data)))
        choice = choices[0] or {}
        message = choice.get("message") or {}
        if not isinstance(message, dict):
            raise self.error(ErrorCode.MODEL_INVALID_RESPONSE, "The model response has an invalid message field.",
                             detail=_truncate(json.dumps(data)))
        raw_content = message.get("content")
        if isinstance(raw_content, list):  # content parts
            raw_content = "".join(p.get("text", "") for p in raw_content if isinstance(p, dict))
        content, inline_reasoning = strip_reasoning(raw_content or "")
        had_reasoning = inline_reasoning or bool(message.get("reasoning_content") or message.get("reasoning"))
        tool_calls: list[ToolCall] = []
        for tc in message.get("tool_calls") or []:
            fn = tc.get("function") or {}
            if not fn.get("name"):
                continue
            raw_args = fn.get("arguments")
            tool_calls.append(
                ToolCall(
                    id=str(tc.get("id") or uuid.uuid4().hex[:12]),
                    name=str(fn["name"]),
                    arguments=parse_tool_arguments(raw_args),
                    raw_arguments=raw_args if isinstance(raw_args, str) else json.dumps(raw_args or {}),
                )
            )
        finish_reason = choice.get("finish_reason")
        if not content.strip() and not tool_calls:
            hint = None
            if had_reasoning:
                hint = ("The model produced only reasoning tokens and no answer. Increase max_tokens "
                        "or configure the server's reasoning parser.")
            elif finish_reason == "length":
                hint = "Generation stopped at max_tokens before any content was produced; increase max_tokens."
            raise self.error(
                ErrorCode.MODEL_EMPTY_RESPONSE,
                f"The model '{self.config.id}' returned an empty response (HTTP 200 with no content).",
                detail=f"finish_reason={finish_reason} raw={_truncate(json.dumps(data), 300)}",
                hint=hint,
            )
        usage_raw = data.get("usage") or {}
        return ChatResponse(
            model_id=self.config.id,
            model_name=str(data.get("model") or self.config.model),
            content=content,
            tool_calls=tool_calls,
            finish_reason=finish_reason,
            usage=Usage(
                prompt_tokens=int(usage_raw.get("prompt_tokens") or 0),
                completion_tokens=int(usage_raw.get("completion_tokens") or 0),
            ),
            latency_ms=latency_ms,
            had_reasoning=had_reasoning,
        )

    async def chat(self, request: ChatRequest) -> ChatResponse:
        payload = self.build_chat_payload(request, stream=False)
        started = time.perf_counter()
        try:
            data = await self._post_json("/chat/completions", payload)
        except ModelError as exc:
            # Some servers reject response_format/json_schema: retry once without it.
            if exc.code == ErrorCode.MODEL_HTTP_ERROR and "response_format" in payload and "400" in exc.message:
                payload.pop("response_format", None)
                data = await self._post_json("/chat/completions", payload)
            else:
                raise
        latency_ms = int((time.perf_counter() - started) * 1000)
        return self.parse_chat_response(data, latency_ms)

    async def stream_chat(self, request: ChatRequest) -> AsyncIterator[ChatChunk]:
        payload = self.build_chat_payload(request, stream=True)
        url = f"{self.base_url}/chat/completions"
        think = ThinkStreamFilter()
        produced = False
        finished = False
        try:
            async with self.http.stream("POST", url, json=payload, headers=self._headers(),
                                        timeout=self.config.timeout_s) as response:
                if response.status_code >= 400:
                    await response.aread()
                    raise self._map_http_error(response)
                async for line in response.aiter_lines():
                    line = line.strip()
                    if not line or line.startswith(":"):
                        continue
                    if not line.startswith("data:"):
                        continue
                    data_str = line[5:].strip()
                    if data_str == "[DONE]":
                        finished = True
                        break
                    try:
                        data = json.loads(data_str)
                    except json.JSONDecodeError as exc:
                        raise self.error(ErrorCode.MODEL_STREAM_ERROR, "The model stream contained malformed data.",
                                         detail=_truncate(data_str)) from exc
                    if "error" in data:
                        raise self.error(ErrorCode.MODEL_STREAM_ERROR, "The model server reported an error during streaming.",
                                         detail=_truncate(json.dumps(data)))
                    usage = None
                    if data.get("usage"):
                        usage = Usage(prompt_tokens=int(data["usage"].get("prompt_tokens") or 0),
                                      completion_tokens=int(data["usage"].get("completion_tokens") or 0))
                    for choice in data.get("choices") or []:
                        delta = choice.get("delta") or {}
                        if delta.get("reasoning_content") or delta.get("reasoning"):
                            think.saw_reasoning = True
                        text = think.feed(delta.get("content") or "")
                        if text:
                            produced = produced or bool(text.strip())
                            yield ChatChunk(content=text)
                        if choice.get("finish_reason"):
                            finished = True
                            yield ChatChunk(finish_reason=choice["finish_reason"])
                    if usage:
                        yield ChatChunk(usage=usage)
        except httpx.HTTPError as exc:
            raise self._map_transport_error(exc, url) from exc
        tail = think.flush()
        if tail:
            produced = produced or bool(tail.strip())
            yield ChatChunk(content=tail)
        if not produced:
            raise self.error(
                ErrorCode.MODEL_EMPTY_RESPONSE,
                f"The model '{self.config.id}' streamed no content.",
                hint="The model produced only reasoning tokens." if think.saw_reasoning else None,
            )
        if not finished:
            raise self.error(ErrorCode.MODEL_STREAM_ERROR, "The model stream ended unexpectedly before completion.")

    async def embed(self, texts: list[str]) -> EmbedResponse:
        if not texts:
            return EmbedResponse(model_id=self.config.id, vectors=[], dimensions=self.config.embedding_dimensions or 0)
        started = time.perf_counter()
        data = await self._post_json("/embeddings", {"model": self.config.model, "input": texts})
        items = data.get("data")
        if not isinstance(items, list) or len(items) != len(texts):
            raise self.error(ErrorCode.MODEL_INVALID_RESPONSE,
                             f"Embedding response has {len(items) if isinstance(items, list) else 'no'} vectors for {len(texts)} inputs.")
        items = sorted(items, key=lambda d: d.get("index", 0))
        vectors = [list(map(float, it.get("embedding") or [])) for it in items]
        return self._validated_embeddings(vectors, started, data.get("usage"))

    def _validated_embeddings(self, vectors: list[list[float]], started: float, usage: Any) -> EmbedResponse:
        if not vectors or any(not v for v in vectors):
            raise self.error(ErrorCode.MODEL_EMPTY_RESPONSE, f"The embedding model '{self.config.id}' returned empty vectors.")
        dims = len(vectors[0])
        if any(len(v) != dims for v in vectors):
            raise self.error(ErrorCode.MODEL_INVALID_RESPONSE, "Embedding vectors have inconsistent dimensions.")
        if self.config.embedding_dimensions and dims != self.config.embedding_dimensions:
            raise self.error(
                ErrorCode.MODEL_INVALID_RESPONSE,
                f"Embedding model '{self.config.id}' returned {dims} dimensions, configured {self.config.embedding_dimensions}.",
                hint="Fix embedding_dimensions in the model configuration (a change requires re-indexing).",
            )
        prompt_tokens = int((usage or {}).get("prompt_tokens") or 0) if isinstance(usage, dict) else 0
        return EmbedResponse(model_id=self.config.id, vectors=vectors, dimensions=dims,
                             latency_ms=int((time.perf_counter() - started) * 1000),
                             usage=Usage(prompt_tokens=prompt_tokens))

    async def rerank(self, query: str, documents: list[str], top_n: int | None = None) -> list[RerankResult]:
        if not documents:
            return []
        payload: dict[str, Any] = {"model": self.config.model, "query": query, "documents": documents}
        if top_n:
            payload["top_n"] = top_n
        data = await self._post_json("/rerank", payload)
        results = data.get("results")
        if not isinstance(results, list) or not results:
            raise self.error(ErrorCode.MODEL_EMPTY_RESPONSE, f"The reranker '{self.config.id}' returned no results.")
        try:
            return [RerankResult(index=int(r["index"]), score=float(r.get("relevance_score", r.get("score", 0.0))))
                    for r in results]
        except (KeyError, TypeError, ValueError) as exc:
            raise self.error(ErrorCode.MODEL_INVALID_RESPONSE, "Reranker response has an unexpected shape.",
                             detail=_truncate(json.dumps(data))) from exc
