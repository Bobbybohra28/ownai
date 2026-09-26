"""Change sets: AI-proposed edits are staged here, never written to the project directly.

A change set records, per file, the proposed new content, a unified diff, and the hash
of the file at proposal time. Applying requires human approval and fails with a
conflict if the file changed since the proposal (the user's work is never overwritten).
"""

from __future__ import annotations

import difflib
import hashlib
import os
import tempfile
import uuid
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import ConflictError, ErrorCode, NotFoundError, ToolError
from app.database.base import utcnow
from app.database.models import ChangeSet, ChangeSetFile
from app.security.paths import PathJail
from app.security.secrets import contains_secret
from app.tools.base import (
    ApprovalRequest,
    PermissionLevel,
    Tool,
    ToolArgs,
    ToolContext,
    ToolOutcome,
)

MAX_FILE_BYTES = 1_000_000


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def file_sha(path: Path) -> str | None:
    if not path.is_file():
        return None
    return hashlib.sha256(path.read_bytes()).hexdigest()


def unified_diff(path: str, old: str | None, new: str | None) -> tuple[str, int, int]:
    old_lines = (old or "").splitlines(keepends=True)
    new_lines = (new or "").splitlines(keepends=True)
    diff = list(difflib.unified_diff(old_lines, new_lines, fromfile=f"a/{path}" if old is not None else "/dev/null",
                                     tofile=f"b/{path}" if new is not None else "/dev/null", n=3))
    text = "".join(line if line.endswith("\n") else line + "\n\\ No newline at end of file\n" for line in diff)
    added = sum(1 for line in diff if line.startswith("+") and not line.startswith("+++"))
    removed = sum(1 for line in diff if line.startswith("-") and not line.startswith("---"))
    return text, added, removed


class ChangeSetService:
    async def get(self, session: AsyncSession, changeset_id: uuid.UUID, org_id: uuid.UUID) -> ChangeSet:
        cs = (await session.execute(select(ChangeSet).where(ChangeSet.id == changeset_id,
                                                            ChangeSet.organization_id == org_id))).scalar_one_or_none()
        if cs is None:
            raise NotFoundError("Change set not found.")
        return cs

    async def for_run(self, session: AsyncSession, *, org_id: uuid.UUID, project_id: uuid.UUID,
                      run_id: uuid.UUID | None, create: bool = True, title: str = "") -> ChangeSet | None:
        if run_id is not None:
            cs = (await session.execute(select(ChangeSet).where(
                ChangeSet.run_id == run_id, ChangeSet.status == "open"))).scalar_one_or_none()
            if cs is not None or not create:
                return cs
        if not create:
            return None
        cs = ChangeSet(organization_id=org_id, project_id=project_id, run_id=run_id, title=title or "Proposed changes")
        session.add(cs)
        await session.flush()
        return cs

    async def files(self, session: AsyncSession, changeset_id: uuid.UUID) -> list[ChangeSetFile]:
        stmt = select(ChangeSetFile).where(ChangeSetFile.changeset_id == changeset_id).order_by(ChangeSetFile.path)
        return list((await session.execute(stmt)).scalars().all())

    async def current_content(self, session: AsyncSession, cs: ChangeSet, jail: PathJail, rel: str) -> str | None:
        """Content of ``rel`` as seen through the change set (staged version wins)."""
        staged = (await session.execute(select(ChangeSetFile).where(
            ChangeSetFile.changeset_id == cs.id, ChangeSetFile.path == rel))).scalar_one_or_none()
        if staged is not None:
            return staged.new_content
        path = jail.resolve(rel)
        if not path.is_file():
            return None
        return path.read_text(encoding="utf-8", errors="replace")

    async def stage(self, session: AsyncSession, cs: ChangeSet, jail: PathJail, rel: str, new_content: str | None) -> ChangeSetFile:
        if cs.status != "open":
            raise ToolError("This change set is no longer open.", code=ErrorCode.CONFLICT)
        rel = jail.normalize(rel)
        target = jail.resolve(rel)
        if target.is_dir():
            raise ToolError(f"'{rel}' is a directory.", code=ErrorCode.TOOL_INVALID_ARGS)
        if new_content is not None:
            if len(new_content.encode("utf-8")) > MAX_FILE_BYTES:
                raise ToolError("Proposed file content is too large.", code=ErrorCode.TOOL_INVALID_ARGS)
            if contains_secret(new_content) and not (target.is_file() and contains_secret(target.read_text(errors="ignore"))):
                raise ToolError("The proposed change appears to add a credential/secret; secrets must come from "
                                "environment variables, not source code.", code=ErrorCode.PERMISSION_DENIED)
        original = target.read_text(encoding="utf-8", errors="replace") if target.is_file() else None
        base_sha = file_sha(target)
        entry = (await session.execute(select(ChangeSetFile).where(
            ChangeSetFile.changeset_id == cs.id, ChangeSetFile.path == rel))).scalar_one_or_none()
        if entry is None:
            entry = ChangeSetFile(changeset_id=cs.id, path=rel, base_sha256=base_sha)
            session.add(entry)
        if original is None and new_content is None:
            raise ToolError(f"Cannot delete '{rel}': it does not exist.", code=ErrorCode.NOT_FOUND)
        entry.operation = "create" if original is None else ("delete" if new_content is None else "modify")
        entry.new_content = new_content
        entry.diff, entry.added_lines, entry.removed_lines = unified_diff(rel, original, new_content)
        await session.flush()
        await self.refresh_stats(session, cs)
        return entry

    async def refresh_stats(self, session: AsyncSession, cs: ChangeSet) -> None:
        files = await self.files(session, cs.id)
        cs.stats = {
            "files": len(files),
            "added": sum(f.added_lines for f in files),
            "removed": sum(f.removed_lines for f in files),
            "paths": [{"path": f.path, "operation": f.operation, "added": f.added_lines, "removed": f.removed_lines}
                      for f in files],
        }

    async def overlay(self, session: AsyncSession, cs: ChangeSet) -> dict[str, str | None]:
        return {f.path: f.new_content for f in await self.files(session, cs.id)}

    async def full_diff(self, session: AsyncSession, cs: ChangeSet) -> str:
        return "".join(f.diff for f in await self.files(session, cs.id))

    async def apply(self, session: AsyncSession, cs: ChangeSet, jail: PathJail, user_id: uuid.UUID | None) -> dict[str, Any]:
        if cs.status != "open":
            raise ConflictError(f"Change set is {cs.status}; only open change sets can be applied.")
        files = await self.files(session, cs.id)
        if not files:
            raise ToolError("The change set is empty.", code=ErrorCode.VALIDATION_ERROR)
        conflicts = [f.path for f in files if file_sha(jail.resolve(f.path)) != f.base_sha256]
        if conflicts:
            cs.status = "conflict"
            raise ConflictError(
                "These files changed after the AI proposed its edits, so nothing was applied: " + ", ".join(conflicts),
                code=ErrorCode.CHANGESET_CONFLICT, data={"conflicts": conflicts},
            )
        written: list[str] = []
        for f in files:
            target = jail.resolve(f.path)
            if f.operation == "delete":
                target.unlink(missing_ok=True)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                fd, tmp = tempfile.mkstemp(dir=target.parent, prefix=".ownai-", suffix=".tmp")
                with os.fdopen(fd, "w", encoding="utf-8", newline="") as fh:
                    fh.write(f.new_content or "")
                if target.exists():
                    os.chmod(tmp, target.stat().st_mode & 0o777)
                os.replace(tmp, target)
            written.append(f.path)
        cs.status = "applied"
        cs.applied_at = utcnow()
        cs.applied_by = user_id
        return {"applied_files": written, "stats": cs.stats}


# ------------------------------------------------------------------------------------------------
# Tools
# ------------------------------------------------------------------------------------------------

class WriteFileArgs(ToolArgs):
    path: str
    content: str


class EditFileArgs(ToolArgs):
    path: str
    find: str
    replace: str


class DeleteFileArgs(ToolArgs):
    path: str


class CreateChangeSetArgs(ToolArgs):
    title: str
    summary: str = ""


async def _stage(ctx: ToolContext, path: str, content: str | None) -> ToolOutcome:
    project = ctx.require_project()
    svc = ctx.services.changesets
    async with ctx.services.sessions() as session:
        cs = await svc.for_run(session, org_id=ctx.org_id, project_id=project.id, run_id=ctx.run_id)
        assert cs is not None
        entry = await svc.stage(session, cs, project.jail, path, content)
        await session.commit()
        return ToolOutcome(
            status="ok",
            summary=f"Staged {entry.operation} of {entry.path} (+{entry.added_lines}/-{entry.removed_lines}) "
                    f"in change set {cs.id} — not applied until the user approves.",
            data={"changeset_id": str(cs.id), "path": entry.path, "operation": entry.operation,
                  "added": entry.added_lines, "removed": entry.removed_lines},
        )


class WriteFileTool(Tool):
    name = "write_file"
    description = "Propose the full new content of a file (creates it if missing). Staged in a change set for review."
    args_model = WriteFileArgs
    permission = PermissionLevel.WRITE_STAGED

    async def run(self, args: WriteFileArgs, ctx: ToolContext) -> ToolOutcome:
        return await _stage(ctx, args.path, args.content)


def _indent_of(line: str) -> str:
    return line[: len(line) - len(line.lstrip())]


def whitespace_tolerant_replace(content: str, find: str, replace: str) -> str | None:
    """Replace a block whose lines match ``find`` ignoring leading indentation.

    Returns the new content, or None when there is not exactly one match. The replacement
    is re-indented by the same offset the matched block has relative to ``find``.
    """
    find_lines = [ln for ln in find.strip("\n").split("\n")]
    target = [ln.strip() for ln in find_lines]
    if not any(target):
        return None
    lines = content.split("\n")
    matches = [i for i in range(len(lines) - len(target) + 1)
               if [ln.strip() for ln in lines[i:i + len(target)]] == target]
    if len(matches) != 1:
        return None
    start = matches[0]
    first_nonblank = next(i for i, t in enumerate(target) if t)
    actual_indent = _indent_of(lines[start + first_nonblank])
    given_indent = _indent_of(find_lines[first_nonblank])
    new_lines = []
    for line in replace.strip("\n").split("\n"):
        if line.strip() and line.startswith(given_indent):
            new_lines.append(actual_indent + line[len(given_indent):])
        elif line.strip():
            new_lines.append(actual_indent + line.lstrip())
        else:
            new_lines.append("")
    return "\n".join(lines[:start] + new_lines + lines[start + len(target):])


def recover_path(project_root: Path, path: str) -> str | None:
    """Map a malformed model-produced path (duplicated segments, missing folders) to a unique project file."""
    cleaned = path.replace("\\", "/").strip().lstrip("./")
    if (project_root / cleaned).is_file():
        return cleaned
    parts = cleaned.split("/")
    for i in range(1, len(parts)):  # e.g. "pkg/a.py/pkg/a.py" -> "pkg/a.py"
        for candidate in ("/".join(parts[:i]), "/".join(parts[i:])):
            if candidate and (project_root / candidate).is_file():
                return candidate
    name = parts[-1]
    matches = [p.relative_to(project_root).as_posix() for p in project_root.rglob(name)
               if p.is_file() and not any(seg in p.parts for seg in (".git", "node_modules", ".venv", "venv"))]
    return matches[0] if len(matches) == 1 else None


class EditFileTool(Tool):
    name = "edit_file"
    description = ("Propose an edit: replace an exact snippet (`find`, must occur exactly once) with `replace`. "
                   "Staged in a change set for review.")
    args_model = EditFileArgs
    permission = PermissionLevel.WRITE_STAGED

    async def run(self, args: EditFileArgs, ctx: ToolContext) -> ToolOutcome:
        project = ctx.require_project()
        path = args.path
        notes: list[str] = []
        if not (project.root / project.jail.normalize(path)).is_file():
            recovered = recover_path(project.root, path)
            if recovered and recovered != path:
                notes.append(f"path '{path}' was interpreted as '{recovered}'")
                path = recovered
        async with ctx.services.sessions() as session:
            cs = await ctx.services.changesets.for_run(session, org_id=ctx.org_id, project_id=project.id,
                                                       run_id=ctx.run_id, create=False)
            if cs is not None:
                current = await ctx.services.changesets.current_content(session, cs, project.jail, path)
            else:
                target = project.jail.resolve(path)
                current = target.read_text(encoding="utf-8", errors="replace") if target.is_file() else None
        if current is None:
            return ToolOutcome(status="error", summary=f"File '{args.path}' does not exist.", error_code="NOT_FOUND")
        count = current.count(args.find) if args.find else 0
        if count == 1:
            updated = current.replace(args.find, args.replace, 1)
        else:
            updated = whitespace_tolerant_replace(current, args.find, args.replace) if count == 0 else None
            if updated is None:
                return ToolOutcome(status="error", error_code="TOOL_INVALID_ARGS",
                                   summary=f"`find` text occurs {count} times in {path}; it must match exactly once "
                                           "(copy it verbatim from the file, including indentation).")
            notes.append("matched ignoring indentation differences")
        outcome = await _stage(ctx, path, updated)
        if notes:
            outcome.summary += " Note: " + "; ".join(notes) + "."
            outcome.data["normalizations"] = notes
        return outcome


class DeleteFileTool(Tool):
    name = "delete_file"
    description = "Propose deleting a file. Staged in a change set; applying it requires approval."
    args_model = DeleteFileArgs
    permission = PermissionLevel.WRITE_STAGED

    async def run(self, args: DeleteFileArgs, ctx: ToolContext) -> ToolOutcome:
        return await _stage(ctx, args.path, None)


class CreateChangeSetTool(Tool):
    name = "create_changeset"
    description = "Create (or title) the change set that collects proposed edits for this task."
    args_model = CreateChangeSetArgs
    permission = PermissionLevel.WRITE_STAGED

    async def run(self, args: CreateChangeSetArgs, ctx: ToolContext) -> ToolOutcome:
        project = ctx.require_project()
        async with ctx.services.sessions() as session:
            cs = await ctx.services.changesets.for_run(session, org_id=ctx.org_id, project_id=project.id,
                                                       run_id=ctx.run_id, title=args.title)
            assert cs is not None
            cs.title = args.title[:400]
            cs.summary = args.summary
            await session.commit()
            return ToolOutcome(status="ok", summary=f"Change set {cs.id} ready.", data={"changeset_id": str(cs.id)})


class ApplyChangeSetArgs(ToolArgs):
    changeset_id: uuid.UUID


class ApplyChangeSetTool(Tool):
    """Applies staged edits to the project files. Always requires human approval."""

    name = "apply_changeset"
    description = "Apply a reviewed change set to the project files (requires user approval)."
    args_model = ApplyChangeSetArgs
    permission = PermissionLevel.DESTRUCTIVE
    timeout_s = 60

    def approval_needed(self, args: ApplyChangeSetArgs, ctx: ToolContext) -> ApprovalRequest | None:
        return ApprovalRequest(title="Apply proposed code changes",
                               description=f"Write the staged edits of change set {args.changeset_id} to the project.",
                               risk_level="high")

    async def run(self, args: ApplyChangeSetArgs, ctx: ToolContext) -> ToolOutcome:
        project = ctx.require_project()
        async with ctx.services.sessions() as session:
            cs = await ctx.services.changesets.get(session, args.changeset_id, ctx.org_id)
            if cs.project_id != project.id:
                raise NotFoundError("Change set not found.")
            try:
                result = await ctx.services.changesets.apply(session, cs, project.jail, ctx.user_id)
            finally:
                await session.commit()
        return ToolOutcome(status="ok", summary=f"Applied {len(result['applied_files'])} file(s).", data=result)
