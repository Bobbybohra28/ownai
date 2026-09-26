"""Provider-neutral request/response types and the ModelProvider interface."""

from __future__ import annotations

import re
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, Field, SecretStr

from app.core.exceptions import ErrorCode, ModelError


class ModelRole(StrEnum):
    FAST = "fast"
    CODING = "coding"
    REASONING = "reasoning"
    VISION = "vision"
    EMBEDDING = "embedding"
    RERANKER = "reranker"


CHAT_ROLES = {ModelRole.FAST, ModelRole.CODING, ModelRole.REASONING, ModelRole.VISION}


class ModelCapabilities(BaseModel):
    supports_chat: bool = True
    supports_streaming: bool = True
    supports_tools: bool = False
    supports_json_schema: bool = True
    supports_vision: bool = False
    supports_reasoning: bool = False
    supports_embeddings: bool = False
    supports_rerank: bool = False


class ModelConfig(BaseModel):
    """One entry in the model registry. Everything here comes from configuration."""

    id: str
    provider: Literal["openai_compatible", "vllm", "ollama"] = "openai_compatible"
    endpoint: str
    api_key: SecretStr | None = None
    model: str
    roles: list[ModelRole] = Field(default_factory=list)
    context_length: int = 8192
    max_output_tokens: int = 2048
    capabilities: ModelCapabilities = Field(default_factory=ModelCapabilities)
    priority: int = 50
    tier: str = "free"
    family: str | None = None
    enabled: bool = True
    timeout_s: float = 180.0
    temperature: float = 0.2
    embedding_dimensions: int | None = None
    # Some embedding models expect task prefixes (e.g. nomic: "search_query: " / "search_document: ").
    embedding_query_prefix: str = ""
    embedding_document_prefix: str = ""
    health_probe_max_tokens: int = 64
    headers: dict[str, str] = Field(default_factory=dict)
    extra_body: dict[str, Any] = Field(default_factory=dict)
    license: str | None = None

    def public_dict(self) -> dict[str, Any]:
        data = self.model_dump(exclude={"api_key", "headers"})
        data["has_api_key"] = bool(self.api_key and self.api_key.get_secret_value())
        return data


class ToolCall(BaseModel):
    id: str
    name: str
    arguments: dict[str, Any]
    raw_arguments: str = ""


class ChatMessage(BaseModel):
    role: Literal["system", "user", "assistant", "tool"]
    content: str | list[dict[str, Any]] | None = None
    tool_calls: list[ToolCall] | None = None
    tool_call_id: str | None = None
    name: str | None = None


class ToolSchema(BaseModel):
    name: str
    description: str
    parameters: dict[str, Any]


class ChatRequest(BaseModel):
    messages: list[ChatMessage]
    tools: list[ToolSchema] | None = None
    response_schema: dict[str, Any] | None = None  # JSON schema for constrained output
    json_mode: bool = False
    temperature: float | None = None
    max_tokens: int | None = None
    stop: list[str] | None = None


class Usage(BaseModel):
    prompt_tokens: int = 0
    completion_tokens: int = 0

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens


class ChatResponse(BaseModel):
    model_id: str
    model_name: str
    content: str
    tool_calls: list[ToolCall] = Field(default_factory=list)
    finish_reason: str | None = None
    usage: Usage = Field(default_factory=Usage)
    latency_ms: int = 0
    had_reasoning: bool = False  # reasoning tokens were produced (never exposed)


class ChatChunk(BaseModel):
    content: str = ""
    finish_reason: str | None = None
    usage: Usage | None = None


class EmbedResponse(BaseModel):
    model_id: str
    vectors: list[list[float]]
    dimensions: int
    latency_ms: int = 0
    usage: Usage = Field(default_factory=Usage)


class RerankResult(BaseModel):
    index: int
    score: float


class HealthCheckStep(BaseModel):
    name: str
    ok: bool
    code: str | None = None
    message: str = ""
    latency_ms: int | None = None


class HealthStatus(StrEnum):
    ONLINE = "online"
    DEGRADED = "degraded"
    OFFLINE = "offline"
    UNKNOWN = "unknown"
    DISABLED = "disabled"


class HealthReport(BaseModel):
    model_id: str
    status: HealthStatus
    checked_at: str
    functional_checked_at: str | None = None  # last time a real completion/embedding was verified
    latency_ms: int | None = None
    error_code: str | None = None
    error: str | None = None
    steps: list[HealthCheckStep] = Field(default_factory=list)
    available_models: list[str] | None = None


class LoadMetrics(BaseModel):
    running: float | None = None
    waiting: float | None = None
    kv_cache_usage: float | None = None


_THINK_BLOCK = re.compile(r"<think>[\s\S]*?(?:</think>|\Z)", re.IGNORECASE)


def strip_reasoning(text: str) -> tuple[str, bool]:
    """Remove ``<think>…</think>`` blocks that some local models emit inline."""
    if "<think>" not in text.lower():
        # some templates emit only the closing tag (reasoning started in the prompt)
        lowered = text.lower()
        if "</think>" in lowered:
            idx = lowered.rindex("</think>")
            return text[idx + len("</think>") :].strip(), True
        return text, False
    cleaned = _THINK_BLOCK.sub("", text)
    return cleaned.strip(), True


class ThinkStreamFilter:
    """Incrementally hides ``<think>`` sections from a token stream."""

    def __init__(self) -> None:
        self._buffer = ""
        self._inside = False
        self.saw_reasoning = False

    def feed(self, text: str) -> str:
        self._buffer += text
        out: list[str] = []
        while self._buffer:
            if self._inside:
                end = self._buffer.lower().find("</think>")
                if end == -1:
                    # keep a tail in case the closing tag is split across chunks
                    self._buffer = self._buffer[-8:]
                    return "".join(out)
                self._buffer = self._buffer[end + len("</think>") :]
                self._inside = False
                continue
            start = self._buffer.lower().find("<think>")
            if start == -1:
                # hold back a possible partial "<think" at the end
                safe = len(self._buffer)
                for k in range(1, 7):
                    if "<think>"[:k] == self._buffer[-k:].lower():
                        safe = len(self._buffer) - k
                out.append(self._buffer[:safe])
                self._buffer = self._buffer[safe:]
                return "".join(out)
            out.append(self._buffer[:start])
            self._buffer = self._buffer[start + len("<think>") :]
            self._inside = True
            self.saw_reasoning = True
        return "".join(out)

    def flush(self) -> str:
        if self._inside:
            self._buffer = ""
            return ""
        rest, self._buffer = self._buffer, ""
        return rest


class ModelProvider(ABC):
    """Talks to exactly one configured model on one endpoint."""

    def __init__(self, config: ModelConfig, http_client: Any) -> None:
        self.config = config
        self.http = http_client
        # populated by list_models() when the server reports it (e.g. vLLM max_model_len)
        self.server_context_lengths: dict[str, int] = {}

    @property
    def model_id(self) -> str:
        return self.config.id

    def error(self, code: ErrorCode, message: str, **kwargs: Any) -> ModelError:
        return ModelError(message, code=code, model_id=self.config.id, **kwargs)

    @abstractmethod
    async def list_models(self) -> list[str]:
        """Return model names served by the endpoint (GET /v1/models or equivalent)."""

    @abstractmethod
    async def chat(self, request: ChatRequest) -> ChatResponse: ...

    @abstractmethod
    def stream_chat(self, request: ChatRequest) -> AsyncIterator[ChatChunk]: ...

    async def embed(self, texts: list[str]) -> EmbedResponse:
        raise self.error(ErrorCode.MODEL_CAPABILITY_UNSUPPORTED, f"Model '{self.config.id}' does not support embeddings.")

    async def rerank(self, query: str, documents: list[str], top_n: int | None = None) -> list[RerankResult]:
        raise self.error(ErrorCode.MODEL_CAPABILITY_UNSUPPORTED, f"Model '{self.config.id}' does not support reranking.")

    async def count_tokens(self, text: str) -> int | None:
        return None

    async def load_metrics(self) -> LoadMetrics | None:
        return None

    def name_matches(self, served: list[str]) -> bool:
        want = self.config.model
        candidates = {want, f"{want}:latest"} if ":" not in want else {want}
        if want.endswith(":latest"):
            candidates.add(want.removesuffix(":latest"))
        return any(s in candidates for s in served)
