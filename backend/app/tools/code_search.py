"""Code search tools: exact/regex search over files and semantic (RAG) search."""

from __future__ import annotations

import fnmatch
import os
import re
from pathlib import Path

from pydantic import Field

from app.core.exceptions import ErrorCode, ToolError
from app.projects.scanner import DEFAULT_IGNORED_DIRS, DEFAULT_IGNORED_FILES
from app.security.secrets import is_sensitive_file, mask_secrets
from app.tools.base import PermissionLevel, Tool, ToolArgs, ToolContext, ToolOutcome


class SearchCodeArgs(ToolArgs):
    pattern: str = Field(min_length=1, max_length=300)
    regex: bool = False
    case_sensitive: bool = False
    path_glob: str | None = Field(default=None, description="e.g. 'src/**/*.py' or '*.ts'")
    max_results: int = Field(default=50, ge=1, le=300)


def iter_text_files(root: Path, path_glob: str | None):
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in DEFAULT_IGNORED_DIRS and not d.endswith(".egg-info")]
        for name in filenames:
            full = Path(dirpath) / name
            rel = full.relative_to(root).as_posix()
            if full.is_symlink() or is_sensitive_file(rel) or any(fnmatch.fnmatch(name, p) for p in DEFAULT_IGNORED_FILES):
                continue
            if path_glob and not (fnmatch.fnmatch(rel, path_glob) or fnmatch.fnmatch(name, path_glob)):
                continue
            try:
                if full.stat().st_size > 1_000_000:
                    continue
                data = full.read_bytes()
            except OSError:
                continue
            if b"\x00" in data[:4096]:
                continue
            yield rel, data.decode("utf-8", errors="replace")


class SearchCodeTool(Tool):
    name = "search_code"
    description = "Search project files for text or a regular expression. Returns file:line matches."
    args_model = SearchCodeArgs
    permission = PermissionLevel.READ
    timeout_s = 60

    async def run(self, args: SearchCodeArgs, ctx: ToolContext) -> ToolOutcome:
        project = ctx.require_project()
        flags = 0 if args.case_sensitive else re.IGNORECASE
        try:
            pattern = re.compile(args.pattern if args.regex else re.escape(args.pattern), flags)
        except re.error as exc:
            raise ToolError(f"Invalid regular expression: {exc}", code=ErrorCode.TOOL_INVALID_ARGS) from exc
        matches: list[dict] = []
        files_hit: set[str] = set()
        for rel, text in iter_text_files(project.root, args.path_glob):
            for idx, line in enumerate(text.split("\n"), start=1):
                if pattern.search(line):
                    matches.append({"file": rel, "line": idx, "text": mask_secrets(line.strip())[:240]})
                    files_hit.add(rel)
                    if len(matches) >= args.max_results:
                        break
            if len(matches) >= args.max_results:
                break
        return ToolOutcome(status="ok", summary=f"{len(matches)} match(es) in {len(files_hit)} file(s)"
                           + (" (limit reached)" if len(matches) >= args.max_results else ""),
                           data={"matches": matches})


class SemanticSearchArgs(ToolArgs):
    query: str = Field(min_length=2, max_length=500)
    top_k: int = Field(default=6, ge=1, le=20)
    language: str | None = None
    path_prefix: str | None = None


class SemanticSearchTool(Tool):
    name = "semantic_search"
    description = ("Find the most relevant code/documentation passages for a natural-language question "
                   "(hybrid semantic + keyword search over the project index). Returns cited passages.")
    args_model = SemanticSearchArgs
    permission = PermissionLevel.READ
    timeout_s = 60

    async def run(self, args: SemanticSearchArgs, ctx: ToolContext) -> ToolOutcome:
        project = ctx.require_project()
        if ctx.services.retriever is None:
            raise ToolError("Search index is not available.", code=ErrorCode.RAG_ERROR)
        result = await ctx.services.retriever.retrieve(
            args.query, org_id=ctx.org_id, project_id=project.id, top_k=args.top_k,
            filters={"language": args.language, "path_prefix": args.path_prefix},
        )
        passages = [{**c.to_dict(), "content": mask_secrets(c.content)[:3000]} for c in result.chunks]
        return ToolOutcome(status="ok", summary=f"{len(passages)} passage(s)"
                           + (f"; {' '.join(result.notices)}" if result.notices else ""),
                           data={"passages": passages, "semantic": result.used_dense, "notices": result.notices})
