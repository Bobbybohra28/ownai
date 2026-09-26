"""Chunking: symbol-aware for code, heading/paragraph-aware for documents.

Chunks keep exact 1-based line ranges so every retrieved passage can be cited as
``file:start-end``.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass

from app.models.tokens import estimate_tokens
from app.projects.symbols import SymbolInfo

MAX_CHUNK_LINES = 120
WINDOW_LINES = 60
WINDOW_OVERLAP = 8
TEXT_CHUNK_CHARS = 1600
TEXT_OVERLAP_CHARS = 200


@dataclass
class Chunk:
    content: str
    start_line: int
    end_line: int
    symbol: str | None = None
    kind: str | None = None

    @property
    def token_count(self) -> int:
        return estimate_tokens(self.content)

    @property
    def content_hash(self) -> str:
        return hashlib.sha256(self.content.encode()).hexdigest()


_IDENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]{2,}")
_CAMEL = re.compile(r"[A-Z]+(?=[A-Z][a-z])|[A-Z]?[a-z]+|[A-Z]+|\d+")


def split_identifier(identifier: str) -> list[str]:
    parts: list[str] = []
    for piece in identifier.split("_"):
        parts.extend(p.lower() for p in _CAMEL.findall(piece))
    return [p for p in parts if len(p) > 1]


def build_search_text(path: str, symbol: str | None, content: str) -> str:
    """Lexical-search text: path, symbol and identifier sub-words make code findable by natural language."""
    words: set[str] = set()
    for ident in _IDENT.findall(content):
        if "_" in ident or any(c.isupper() for c in ident[1:]):
            words.update(split_identifier(ident))
    path_words = " ".join(re.split(r"[/._\-]", path))
    symbol_words = " ".join(split_identifier(symbol)) if symbol else ""
    return f"{path} {path_words} {symbol or ''} {symbol_words}\n{content}\n{' '.join(sorted(words))}"


def _windows(lines: list[str], start: int, end: int, symbol: str | None, kind: str | None) -> list[Chunk]:
    """Split lines[start-1:end] (1-based inclusive) into overlapping windows."""
    chunks: list[Chunk] = []
    cursor = start
    while cursor <= end:
        stop = min(end, cursor + WINDOW_LINES - 1)
        text = "\n".join(lines[cursor - 1: stop])
        if text.strip():
            chunks.append(Chunk(text, cursor, stop, symbol, kind))
        if stop >= end:
            break
        cursor = stop - WINDOW_OVERLAP + 1
    return chunks


def chunk_code(text: str, symbols: list[SymbolInfo]) -> list[Chunk]:
    lines = text.split("\n")
    total = len(lines)
    if total == 0 or not text.strip():
        return []
    chunks: list[Chunk] = []
    covered = [False] * (total + 2)

    def emit_symbol(sym: SymbolInfo) -> None:
        start, end = max(1, sym.start_line), min(total, sym.end_line)
        if end < start:
            return
        span = end - start + 1
        children = [s for s in symbols if s.parent == sym.qualified_name and s.start_line >= start and s.end_line <= end]
        if span <= MAX_CHUNK_LINES:
            chunks.append(Chunk("\n".join(lines[start - 1:end]), start, end, sym.qualified_name, sym.kind))
        elif children:
            # large class: header chunk + one chunk per member
            first_child = min(c.start_line for c in children)
            header_end = min(first_child - 1, start + WINDOW_LINES - 1)
            if header_end >= start:
                chunks.append(Chunk("\n".join(lines[start - 1:header_end]), start, header_end, sym.qualified_name, sym.kind))
            for child in children:
                emit_symbol(child)
        else:
            chunks.extend(_windows(lines, start, end, sym.qualified_name, sym.kind))
        for i in range(start, end + 1):
            covered[i] = True

    for sym in symbols:
        if sym.parent is None and sym.kind not in {"constant", "key"}:
            emit_symbol(sym)

    # uncovered regions: imports, module-level code, configuration
    region_start: int | None = None
    for line_no in range(1, total + 2):
        is_free = line_no <= total and not covered[line_no]
        if is_free and region_start is None:
            region_start = line_no
        elif not is_free and region_start is not None:
            chunks.extend(_windows(lines, region_start, line_no - 1, None, "module"))
            region_start = None
    chunks.sort(key=lambda c: (c.start_line, c.end_line))
    return chunks


def chunk_text(text: str, headings: list[SymbolInfo] | None = None) -> list[Chunk]:
    """Paragraph-packing chunker for prose; sections come from markdown headings when available."""
    lines = text.split("\n")
    if not text.strip():
        return []
    sections: list[tuple[int, int, str | None]] = []
    top = sorted([h for h in headings or [] if h.kind == "section"], key=lambda h: h.start_line)
    if top:
        boundaries = [h.start_line for h in top]
        if boundaries[0] > 1:
            sections.append((1, boundaries[0] - 1, None))
        for i, h in enumerate(top):
            end = (boundaries[i + 1] - 1) if i + 1 < len(boundaries) else len(lines)
            sections.append((h.start_line, end, h.name))
    else:
        sections.append((1, len(lines), None))

    chunks: list[Chunk] = []
    for sec_start, sec_end, title in sections:
        buf: list[str] = []
        buf_start = sec_start
        size = 0
        emitted_upto = sec_start - 1
        for line_no in range(sec_start, sec_end + 1):
            line = lines[line_no - 1]
            buf.append(line)
            size += len(line) + 1
            paragraph_break = not line.strip()
            if size >= TEXT_CHUNK_CHARS and (paragraph_break or size >= TEXT_CHUNK_CHARS * 1.5):
                content = "\n".join(buf).strip("\n")
                if content.strip():
                    chunks.append(Chunk(content, buf_start, line_no, title, "section" if title else "text"))
                    emitted_upto = line_no
                # overlap: carry trailing lines worth ~TEXT_OVERLAP_CHARS
                carry: list[str] = []
                carried = 0
                for prev in reversed(buf):
                    if carried >= TEXT_OVERLAP_CHARS:
                        break
                    carry.insert(0, prev)
                    carried += len(prev) + 1
                buf = carry
                buf_start = line_no - len(carry) + 1
                size = carried
        content = "\n".join(buf).strip("\n")
        if content.strip() and sec_end > emitted_upto:
            chunks.append(Chunk(content, buf_start, sec_end, title, "section" if title else "text"))
    return chunks
