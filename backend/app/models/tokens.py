"""Token estimation and budgeting.

Exact counts come from the serving endpoint when it offers a tokenizer API (vLLM
``/tokenize``); otherwise a conservative character-based estimate is used. The
estimate deliberately over-counts so context budgets are not exceeded.
"""

from __future__ import annotations

import math
from collections.abc import Iterable

from app.models.providers.base import ChatMessage

CHARS_PER_TOKEN = 3.2
MESSAGE_OVERHEAD_TOKENS = 6


def estimate_tokens(text: str | None) -> int:
    if not text:
        return 0
    return math.ceil(len(text) / CHARS_PER_TOKEN)


def estimate_messages(messages: Iterable[ChatMessage]) -> int:
    total = 0
    for m in messages:
        content = m.content if isinstance(m.content, str) else str(m.content or "")
        total += estimate_tokens(content) + MESSAGE_OVERHEAD_TOKENS
        for tc in m.tool_calls or []:
            total += estimate_tokens(tc.raw_arguments or str(tc.arguments)) + 4
    return total + 3


def truncate_to_tokens(text: str, max_tokens: int, *, marker: str = "\n…[truncated]") -> str:
    if estimate_tokens(text) <= max_tokens:
        return text
    limit = max(0, int(max_tokens * CHARS_PER_TOKEN) - len(marker))
    return text[:limit] + marker


class TokenBudget:
    """Tracks remaining tokens while assembling a prompt."""

    def __init__(self, total: int) -> None:
        self.total = total
        self.used = 0

    @property
    def remaining(self) -> int:
        return max(0, self.total - self.used)

    def try_consume(self, text: str) -> bool:
        cost = estimate_tokens(text)
        if cost > self.remaining:
            return False
        self.used += cost
        return True
