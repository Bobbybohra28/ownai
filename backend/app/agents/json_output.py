"""Robust extraction of JSON objects from model output."""

from __future__ import annotations

import json
import re
from typing import Any

_FENCE = re.compile(r"```(?:json|JSON)?\s*\n?([\s\S]*?)```")
_TRAILING_COMMA = re.compile(r",\s*([}\]])")


def _balanced_object(text: str, start: int) -> str | None:
    depth = 0
    in_string = False
    escape = False
    for i in range(start, len(text)):
        ch = text[i]
        if in_string:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return text[start:i + 1]
    return None


def extract_json_object(text: str) -> dict[str, Any] | None:
    """Return the first JSON object found in ``text`` (fenced or bare), or None."""
    if not text:
        return None
    candidates: list[str] = [m.group(1).strip() for m in _FENCE.finditer(text)]
    stripped = text.strip()
    if stripped.startswith("{"):
        candidates.insert(0, stripped)
    for idx in [m.start() for m in re.finditer(r"\{", text)][:20]:
        obj = _balanced_object(text, idx)
        if obj:
            candidates.append(obj)
    for candidate in candidates:
        for attempt in (candidate, _TRAILING_COMMA.sub(r"\1", candidate)):
            try:
                value = json.loads(attempt)
            except (json.JSONDecodeError, ValueError):
                continue
            if isinstance(value, dict):
                return value
    return None


def compact_schema(schema: dict[str, Any], defs: dict[str, Any] | None = None, depth: int = 0) -> Any:
    """Human/LLM-readable skeleton of a JSON schema (smaller than the full schema)."""
    defs = defs if defs is not None else schema.get("$defs", {})
    if depth > 6:
        return "..."
    if "$ref" in schema:
        return compact_schema(defs.get(schema["$ref"].split("/")[-1], {}), defs, depth + 1)
    if "anyOf" in schema:
        options = [compact_schema(s, defs, depth + 1) for s in schema["anyOf"] if s.get("type") != "null"]
        return options[0] if len(options) == 1 else options
    kind = schema.get("type")
    if "enum" in schema:
        return " | ".join(map(str, schema["enum"]))
    if kind == "object" or "properties" in schema:
        return {k: compact_schema(v, defs, depth + 1) for k, v in schema.get("properties", {}).items()}
    if kind == "array":
        return [compact_schema(schema.get("items", {}), defs, depth + 1)]
    return kind or "any"
