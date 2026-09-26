"""File reading and project inspection tools (read-only)."""

from __future__ import annotations

from pydantic import Field

from app.projects.scanner import render_tree, scan_project
from app.security.secrets import find_secrets, is_env_file, list_env_keys, mask_secrets
from app.tools.base import PermissionLevel, Tool, ToolArgs, ToolContext, ToolOutcome

MAX_READ_LINES = 400
MAX_READ_CHARS = 40_000


class ReadFileArgs(ToolArgs):
    path: str = Field(description="Project-relative path")
    start_line: int = Field(default=1, ge=1)
    end_line: int | None = Field(default=None, ge=1)


class ReadFileTool(Tool):
    name = "read_file"
    description = ("Read a project file (optionally a line range). Returns numbered lines. Secrets are masked; "
                   "credential files (.env, keys) cannot be read.")
    args_model = ReadFileArgs
    permission = PermissionLevel.READ

    async def run(self, args: ReadFileArgs, ctx: ToolContext) -> ToolOutcome:
        project = ctx.require_project()
        jail = project.jail
        rel = jail.normalize(args.path)
        if is_env_file(rel):
            target = jail.resolve(rel, allow_sensitive=True, must_exist=True)
            keys = list_env_keys(target.read_text(encoding="utf-8", errors="ignore"))
            return ToolOutcome(status="ok", summary=f"{rel} defines {len(keys)} variables (values hidden).",
                               data={"path": rel, "variables": keys, "values_hidden": True})
        target = jail.resolve(rel, must_exist=True)
        if not target.is_file():
            return ToolOutcome(status="error", summary=f"'{rel}' is not a file.", error_code="TOOL_INVALID_ARGS")
        raw = target.read_bytes()
        if b"\x00" in raw[:4096]:
            return ToolOutcome(status="error", summary=f"'{rel}' is a binary file.", error_code="TOOL_INVALID_ARGS")
        text = raw.decode("utf-8", errors="replace")
        lines = text.split("\n")
        start = args.start_line
        end = min(args.end_line or len(lines), len(lines), start + MAX_READ_LINES - 1)
        selected = lines[start - 1:end]
        body = "\n".join(f"{start + i:>5} | {line}" for i, line in enumerate(selected))
        masked = mask_secrets(body)
        truncated = end < (args.end_line or len(lines)) or len(masked) > MAX_READ_CHARS
        return ToolOutcome(
            status="ok",
            summary=f"{rel} lines {start}-{end} of {len(lines)}" + (" (truncated)" if truncated else ""),
            data={"path": rel, "start_line": start, "end_line": end, "total_lines": len(lines),
                  "content": masked[:MAX_READ_CHARS], "secrets_masked": len(find_secrets(body))},
        )


class ProjectStructureArgs(ToolArgs):
    path: str = ""
    depth: int = Field(default=3, ge=1, le=8)


class ProjectStructureTool(Tool):
    name = "project_structure"
    description = "Show the project's directory tree (ignores dependency/build folders)."
    args_model = ProjectStructureArgs
    permission = PermissionLevel.READ

    async def run(self, args: ProjectStructureArgs, ctx: ToolContext) -> ToolOutcome:
        project = ctx.require_project()
        base = project.jail.resolve(args.path or "", allow_sensitive=True, must_exist=True)
        scan = scan_project(base, max_files=5000)
        tree = render_tree([f.path for f in scan.files], max_depth=args.depth, max_entries=400)
        return ToolOutcome(status="ok", summary=f"{len(scan.files)} files under /{args.path}",
                           data={"tree": tree, "file_count": len(scan.files)})


class ScanProjectArgs(ToolArgs):
    pass


class ScanProjectTool(Tool):
    name = "scan_project"
    description = ("Summarise the project: languages, frameworks, dependencies, API endpoints, environment "
                   "variables (names only), test setup, and files changed since the last index.")
    args_model = ScanProjectArgs
    permission = PermissionLevel.READ
    timeout_s = 120

    async def run(self, args: ScanProjectArgs, ctx: ToolContext) -> ToolOutcome:
        from sqlalchemy import select

        from app.database.models import ProjectFile

        project = ctx.require_project()
        scan = scan_project(project.root, max_files=ctx.services.settings.max_project_files)
        async with ctx.services.sessions() as session:
            indexed = {p: h for p, h in (await session.execute(
                select(ProjectFile.path, ProjectFile.sha256).where(ProjectFile.project_id == project.id))).all()}
        changed = [f.path for f in scan.files if indexed.get(f.path) != f.sha256]
        removed = sorted(set(indexed) - {f.path for f in scan.files})
        ov = project.overview or {}
        return ToolOutcome(status="ok", summary=f"{len(scan.files)} files; {len(changed)} changed since last index.", data={
            "languages": scan.languages,
            "frameworks": ov.get("frameworks", []),
            "dependencies": [f"{d['name']}{' ' + d['version'] if d.get('version') else ''}"
                             for d in ov.get("dependencies", [])][:80],
            "endpoints": [f"{e['method']} {e['path']} ({e['file']}:{e['line']})" for e in ov.get("endpoints", [])][:60],
            "env_vars": ov.get("env_vars", []),
            "tests": ov.get("tests", {}),
            "changed_since_index": changed[:100],
            "removed_since_index": removed[:100],
            "sensitive_files": ov.get("sensitive_files", []),
        })
