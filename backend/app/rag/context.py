"""Context assembly: merges retrieved chunks into a token-budgeted, citable context block."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from app.models.tokens import estimate_tokens
from app.rag.retrieval import RetrievedChunk
from app.security.secrets import mask_secrets


@dataclass
class Citation:
    id: str  # "S1"
    file_path: str
    start_line: int
    end_line: int
    symbol: str | None = None
    document_type: str = "code"
    score: float = 0.0

    def label(self) -> str:
        return f"{self.file_path}:{self.start_line}-{self.end_line}"

    def to_dict(self) -> dict[str, Any]:
        return {"id": self.id, "file_path": self.file_path, "start_line": self.start_line, "end_line": self.end_line,
                "symbol": self.symbol, "document_type": self.document_type, "score": round(self.score, 4)}


@dataclass
class ContextPack:
    text: str
    citations: list[Citation] = field(default_factory=list)
    token_count: int = 0
    notices: list[str] = field(default_factory=list)

    @property
    def empty(self) -> bool:
        return not self.citations


def _merge(chunks: list[RetrievedChunk]) -> list[RetrievedChunk]:
    """Merge overlapping/adjacent chunks from the same file to avoid duplicate text."""
    by_file: dict[str, list[RetrievedChunk]] = {}
    order: list[str] = []
    for c in chunks:
        if c.file_path not in by_file:
            order.append(c.file_path)
        by_file.setdefault(c.file_path, []).append(c)
    merged: list[RetrievedChunk] = []
    for path in order:
        items = sorted(by_file[path], key=lambda c: c.start_line)
        current = items[0]
        for nxt in items[1:]:
            if nxt.start_line <= current.end_line + 1:
                if nxt.end_line > current.end_line:
                    cur_lines = current.content.split("\n")
                    nxt_lines = nxt.content.split("\n")
                    overlap = current.end_line - nxt.start_line + 1
                    current.content = "\n".join(cur_lines + nxt_lines[overlap:])
                    current.end_line = nxt.end_line
                current.score = max(current.score, nxt.score)
                current.symbol = current.symbol or nxt.symbol
            else:
                merged.append(current)
                current = nxt
        merged.append(current)
    return sorted(merged, key=lambda c: -c.score)


def build_context(chunks: list[RetrievedChunk], *, budget_tokens: int, header: str = "") -> ContextPack:
    blocks: list[str] = []
    citations: list[Citation] = []
    used = estimate_tokens(header)
    for chunk in _merge(chunks):
        cid = f"S{len(citations) + 1}"
        label = f"{chunk.file_path}:{chunk.start_line}-{chunk.end_line}"
        meta = f" ({chunk.symbol})" if chunk.symbol else ""
        fence = chunk.language if chunk.document_type == "code" and chunk.language else ""
        block = f"[{cid}] {label}{meta}\n```{fence}\n{mask_secrets(chunk.content)}\n```"
        cost = estimate_tokens(block)
        if used + cost > budget_tokens:
            if citations:
                continue  # try smaller chunks further down the list
            # always include at least a truncated first chunk
            allowed = max(200, budget_tokens - used) * 3
            block = block[: int(allowed)] + "\n…```"
            cost = estimate_tokens(block)
        blocks.append(block)
        used += cost
        citations.append(Citation(cid, chunk.file_path, chunk.start_line, chunk.end_line, chunk.symbol,
                                  chunk.document_type, chunk.score))
    text = (header + "\n\n" if header else "") + "\n\n".join(blocks)
    return ContextPack(text=text, citations=citations, token_count=used)
