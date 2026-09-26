"""Git tools. Git runs with hooks/fsmonitor/external diff disabled so repository
content cannot execute programs on the host. Commits and checkouts need approval;
nothing here pushes, resets or deletes history."""

from __future__ import annotations

import asyncio
import os
import re
from pathlib import Path

from pydantic import Field

from app.core.exceptions import ErrorCode, ToolError
from app.security.secrets import mask_secrets
from app.tools.base import ApprovalRequest, PermissionLevel, Tool, ToolArgs, ToolContext, ToolOutcome

_SAFE_CONFIG = [
    "-c", "core.hooksPath=/dev/null", "-c", "core.fsmonitor=false", "-c", "core.pager=cat",
    "-c", "diff.external=", "-c", "core.sshCommand=/bin/false", "-c", "credential.helper=",
    "-c", "protocol.allow=never", "-c", "core.askPass=/bin/false",
]
_BRANCH = re.compile(r"^(?!-)(?!.*\.\.)[\w./\-]{1,200}$")


async def run_git(root: Path, *args: str, timeout: int = 30, check: bool = True) -> str:
    if not (root / ".git").exists():
        raise ToolError("This project is not a Git repository.", code=ErrorCode.GIT_ERROR)
    env = {k: v for k, v in os.environ.items() if k in {"PATH", "HOME", "LANG", "LC_ALL"}}
    env.update({"GIT_TERMINAL_PROMPT": "0", "GIT_CONFIG_NOSYSTEM": "1", "GIT_OPTIONAL_LOCKS": "0",
                "GIT_AUTHOR_NAME": env.get("GIT_AUTHOR_NAME", "OwnAI"), "GIT_COMMITTER_NAME": "OwnAI",
                "GIT_AUTHOR_EMAIL": "ownai@localhost", "GIT_COMMITTER_EMAIL": "ownai@localhost"})
    proc = await asyncio.create_subprocess_exec("git", *_SAFE_CONFIG, *args, cwd=root, env=env,
                                                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except TimeoutError as exc:
        proc.kill()
        raise ToolError("Git command timed out.", code=ErrorCode.TOOL_TIMEOUT) from exc
    if check and proc.returncode != 0:
        raise ToolError(f"git {args[0]} failed: {err.decode(errors='replace').strip()[:300]}", code=ErrorCode.GIT_ERROR)
    return out.decode("utf-8", errors="replace")


class NoArgs(ToolArgs):
    pass


class GitStatusTool(Tool):
    name = "git_status"
    description = "Show the current branch and modified/untracked files."
    args_model = NoArgs

    async def run(self, args: NoArgs, ctx: ToolContext) -> ToolOutcome:
        root = ctx.require_project().root
        out = await run_git(root, "status", "--porcelain=v1", "--branch", "--untracked-files=normal")
        lines = out.splitlines()
        branch = lines[0][3:] if lines and lines[0].startswith("## ") else "unknown"
        files = [{"status": ln[:2].strip() or "?", "path": ln[3:]} for ln in lines[1:]]
        return ToolOutcome(status="ok", summary=f"On {branch}; {len(files)} changed file(s)",
                           data={"branch": branch, "files": files[:500], "clean": not files})


class GitDiffArgs(ToolArgs):
    path: str | None = None
    staged: bool = False
    ref: str | None = Field(default=None, description="Compare against this commit/branch")


class GitDiffTool(Tool):
    name = "git_diff"
    description = "Show uncommitted changes (or changes against a ref), optionally for one path."
    args_model = GitDiffArgs

    async def run(self, args: GitDiffArgs, ctx: ToolContext) -> ToolOutcome:
        project = ctx.require_project()
        cmd = ["diff", "--no-ext-diff", "--no-textconv", "--no-color"]
        if args.staged:
            cmd.append("--cached")
        if args.ref:
            if not _BRANCH.match(args.ref):
                raise ToolError("Invalid ref.", code=ErrorCode.TOOL_INVALID_ARGS)
            cmd.append(args.ref)
        cmd.append("--")
        if args.path:
            cmd.append(project.jail.normalize(args.path))
        out = await run_git(project.root, *cmd)
        return ToolOutcome(status="ok", summary=f"{out.count(chr(10) + 'diff --git') + (1 if out.startswith('diff') else 0)} file(s) differ",
                           data={"diff": mask_secrets(out[:60_000]), "truncated": len(out) > 60_000})


class GitLogArgs(ToolArgs):
    limit: int = Field(default=20, ge=1, le=200)
    path: str | None = None


class GitLogTool(Tool):
    name = "git_log"
    description = "Show recent commits (hash, author, date, subject)."
    args_model = GitLogArgs

    async def run(self, args: GitLogArgs, ctx: ToolContext) -> ToolOutcome:
        project = ctx.require_project()
        cmd = ["log", f"-n{args.limit}", "--no-color", "--date=iso-strict", "--pretty=format:%H%x1f%an%x1f%ad%x1f%s"]
        if args.path:
            cmd += ["--", project.jail.normalize(args.path)]
        out = await run_git(project.root, *cmd, check=False)
        commits = []
        for line in out.splitlines():
            parts = line.split("\x1f")
            if len(parts) == 4:
                commits.append({"hash": parts[0][:12], "author": parts[1], "date": parts[2], "subject": parts[3]})
        return ToolOutcome(status="ok", summary=f"{len(commits)} commit(s)", data={"commits": commits})


class GitBranchArgs(ToolArgs):
    create: str | None = Field(default=None, description="Create a new branch with this name (from HEAD)")


class GitBranchTool(Tool):
    name = "git_branch"
    description = "List branches, or create a new branch (does not switch to it)."
    args_model = GitBranchArgs

    async def run(self, args: GitBranchArgs, ctx: ToolContext) -> ToolOutcome:
        root = ctx.require_project().root
        if args.create:
            if not _BRANCH.match(args.create):
                raise ToolError("Invalid branch name.", code=ErrorCode.TOOL_INVALID_ARGS)
            await run_git(root, "branch", "--", args.create)
        out = await run_git(root, "branch", "--list", "--no-color", "--format=%(HEAD)%(refname:short)")
        branches = [{"name": ln[1:], "current": ln.startswith("*")} for ln in out.splitlines() if ln.strip()]
        return ToolOutcome(status="ok", summary=f"{len(branches)} branch(es)" + (f"; created {args.create}" if args.create else ""),
                           data={"branches": branches})


class GitCheckoutArgs(ToolArgs):
    branch: str


class GitCheckoutTool(Tool):
    name = "git_checkout"
    description = "Switch to another branch. Refused if there are uncommitted changes. Requires approval."
    args_model = GitCheckoutArgs
    permission = PermissionLevel.DESTRUCTIVE

    def approval_needed(self, args: GitCheckoutArgs, ctx: ToolContext) -> ApprovalRequest | None:
        return ApprovalRequest(title=f"Switch Git branch to '{args.branch}'",
                               description="Changes the files in the working tree.", risk_level="medium")

    async def run(self, args: GitCheckoutArgs, ctx: ToolContext) -> ToolOutcome:
        root = ctx.require_project().root
        if not _BRANCH.match(args.branch):
            raise ToolError("Invalid branch name.", code=ErrorCode.TOOL_INVALID_ARGS)
        status = await run_git(root, "status", "--porcelain=v1", "--untracked-files=no")
        if status.strip():
            return ToolOutcome(status="error", error_code="GIT_DIRTY",
                               summary="There are uncommitted changes; checkout refused to protect your work.")
        await run_git(root, "switch", "--", args.branch)
        return ToolOutcome(status="ok", summary=f"Switched to {args.branch}", data={"branch": args.branch})


class GitCommitArgs(ToolArgs):
    message: str = Field(min_length=3, max_length=2000)
    paths: list[str] = Field(default_factory=list, description="Files to commit (default: all tracked changes)")


class GitCommitTool(Tool):
    name = "git_commit"
    description = "Commit changes to the local repository (never pushes). Requires approval."
    args_model = GitCommitArgs
    permission = PermissionLevel.DESTRUCTIVE

    def approval_needed(self, args: GitCommitArgs, ctx: ToolContext) -> ApprovalRequest | None:
        return ApprovalRequest(title="Create a Git commit",
                               description=f"Commit {'selected files' if args.paths else 'all tracked changes'}: "
                                           f"\"{args.message[:120]}\"", risk_level="medium")

    async def run(self, args: GitCommitArgs, ctx: ToolContext) -> ToolOutcome:
        project = ctx.require_project()
        paths = [project.jail.normalize(p) for p in args.paths]
        if paths:
            await run_git(project.root, "add", "--", *paths)
        else:
            await run_git(project.root, "add", "--update")
        await run_git(project.root, "commit", "--no-verify", "-m", args.message)
        head = (await run_git(project.root, "rev-parse", "--short", "HEAD")).strip()
        return ToolOutcome(status="ok", summary=f"Committed {head}", data={"commit": head})
