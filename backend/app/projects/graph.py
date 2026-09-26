"""Import resolution and a lightweight file-level dependency graph."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import PurePosixPath

_JS_EXTS = (".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs", ".vue", ".svelte")


def _python_candidates(importer: str, module: str) -> list[str]:
    if module.startswith("."):
        level = len(module) - len(module.lstrip("."))
        rest = module.lstrip(".")
        base = PurePosixPath(importer).parent
        for _ in range(level - 1):
            base = base.parent
        parts = [p for p in rest.split(".") if p]
        target = base.joinpath(*parts) if parts else base
        stem = target.as_posix()
        return [f"{stem}.py", f"{stem}/__init__.py"]
    parts = module.split(".")
    stem = "/".join(parts)
    return [f"{stem}.py", f"{stem}/__init__.py"]


def _js_candidates(importer: str, module: str) -> list[str]:
    if not module.startswith("."):
        return []
    base = PurePosixPath(importer).parent
    target = PurePosixPath(*(base / module).parts)
    parts: list[str] = []
    for part in target.parts:
        if part == "..":
            if parts:
                parts.pop()
        elif part != ".":
            parts.append(part)
    stem = "/".join(parts)
    candidates = [stem]
    candidates += [stem + ext for ext in _JS_EXTS]
    candidates += [f"{stem}/index{ext}" for ext in _JS_EXTS]
    return candidates


@dataclass
class ResolvedImport:
    source: str
    ref: str
    target: str | None

    @property
    def external(self) -> bool:
        return self.target is None


@dataclass
class ProjectGraph:
    edges: dict[str, set[str]] = field(default_factory=lambda: defaultdict(set))
    reverse: dict[str, set[str]] = field(default_factory=lambda: defaultdict(set))

    def add(self, source: str, target: str) -> None:
        if source != target:
            self.edges[source].add(target)
            self.reverse[target].add(source)

    def neighbors(self, path: str, *, depth: int = 1) -> set[str]:
        seen: set[str] = set()
        frontier = {path}
        for _ in range(depth):
            nxt: set[str] = set()
            for p in frontier:
                nxt |= self.edges.get(p, set()) | self.reverse.get(p, set())
            nxt -= seen | {path}
            seen |= nxt
            frontier = nxt
        return seen


def resolve_imports(importer: str, language: str | None, imports: list[str], all_files: set[str]) -> list[ResolvedImport]:
    resolved: list[ResolvedImport] = []
    for ref in imports:
        target: str | None = None
        if language == "python":
            candidates = _python_candidates(importer, ref)
            target = next((c for c in candidates if c in all_files), None)
            if target is None and not ref.startswith("."):
                # nested source roots (e.g. backend/app/...): accept a unique suffix match
                for cand in candidates:
                    matches = [f for f in all_files if f.endswith("/" + cand)]
                    if len(matches) == 1:
                        target = matches[0]
                        break
        elif language in {"javascript", "typescript", "jsx", "tsx", "vue", "svelte"}:
            target = next((c for c in _js_candidates(importer, ref) if c in all_files), None)
        resolved.append(ResolvedImport(importer, ref, target))
    return resolved
