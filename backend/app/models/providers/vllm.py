"""vLLM provider: OpenAI-compatible API plus vLLM-specific tokenizer and load metrics."""

from __future__ import annotations

import re

import httpx

from app.core.logging import get_logger
from app.models.providers.base import LoadMetrics
from app.models.providers.openai_compatible import OpenAICompatibleProvider

log = get_logger(__name__)

_METRIC_LINE = re.compile(r"^(vllm:[a-z_]+)(?:\{[^}]*\})?\s+([0-9.eE+\-]+)$")


class VLLMProvider(OpenAICompatibleProvider):
    @property
    def server_root(self) -> str:
        base = self.base_url
        return base[: -len("/v1")] if base.endswith("/v1") else base

    async def count_tokens(self, text: str) -> int | None:
        try:
            response = await self.http.post(
                f"{self.server_root}/tokenize",
                json={"model": self.config.model, "prompt": text},
                headers=self._headers(),
                timeout=10,
            )
            if response.status_code != 200:
                return None
            data = response.json()
            if "count" in data:
                return int(data["count"])
            return len(data.get("tokens") or [])
        except (httpx.HTTPError, ValueError) as exc:
            log.debug("vllm.tokenize_failed", model_id=self.config.id, error=str(exc))
            return None

    async def load_metrics(self) -> LoadMetrics | None:
        try:
            response = await self.http.get(f"{self.server_root}/metrics", timeout=5)
        except httpx.HTTPError:
            return None
        if response.status_code != 200:
            return None
        values: dict[str, float] = {}
        for line in response.text.splitlines():
            match = _METRIC_LINE.match(line.strip())
            if match:
                values[match.group(1)] = values.get(match.group(1), 0.0) + float(match.group(2))
        kv = values.get("vllm:kv_cache_usage_perc", values.get("vllm:gpu_cache_usage_perc"))
        return LoadMetrics(
            running=values.get("vllm:num_requests_running"),
            waiting=values.get("vllm:num_requests_waiting"),
            kv_cache_usage=kv,
        )
