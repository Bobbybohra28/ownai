"""Sandboxed execution tools: run_python, run_tests, run_lint.

All execution happens in the sandbox runner (isolated container, no network).
Results are parsed into structured data so verification never relies on model text.
"""

from __future__ import annotations

import re
from pathlib import PurePosixPath
from typing import Any

from pydantic import Field

from app.core.exceptions import ErrorCode, ToolError
from app.sandbox.client import SandboxResult
from app.security.secrets import mask_secrets
from app.tools.base import PermissionLevel, Tool, ToolArgs, ToolContext, ToolOutcome

_PYTEST_COUNTS = re.compile(r"(\d+) (passed|failed|errors?|skipped|xfailed|xpassed|deselected|warnings?)")
_PYTEST_FAILED = re.compile(r"^(FAILED|ERROR) (\S+)(?: - (.*))?$", re.MULTILINE)
_RUFF_LINE = re.compile(r"^(?P<file>[^:\n]+):(?P<line>\d+):(?P<col>\d+): (?P<code>[A-Z]+\d+) (?P<msg>.*)$", re.MULTILINE)


def parse_pytest(output: str) -> dict[str, Any]:
    counts: dict[str, int] = {}
    summary_lines = [ln for ln in output.splitlines() if re.search(r"\d+ (passed|failed|error)", ln)]
    if summary_lines:
        for number, kind in _PYTEST_COUNTS.findall(summary_lines[-1]):
            key = {"error": "errors", "warning": "warnings"}.get(kind, kind)
            counts[key] = int(number)
    failures = [{"kind": k.lower(), "test": t, "message": (m or "")[:300]} for k, t, m in _PYTEST_FAILED.findall(output)]
    return {"counts": counts, "failures": failures[:50],
            "no_tests_collected": "no tests ran" in output or "collected 0 items" in output}


def _outcome_from(result: SandboxResult, *, label: str, parsed: dict[str, Any] | None = None) -> dict[str, Any]:
    return {
        "exit_code": result.exit_code, "timed_out": result.timed_out, "oom_killed": result.oom_killed,
        "duration_ms": result.duration_ms, "stdout": mask_secrets(result.stdout[-12000:]),
        "stderr": mask_secrets(result.stderr[-6000:]), "truncated": result.truncated, "image": result.image,
        "label": label, **(parsed or {}),
    }


async def _overlay_for(ctx: ToolContext, use_changeset: bool) -> tuple[dict[str, str | None] | None, str | None]:
    if not use_changeset or ctx.run_id is None or ctx.project is None:
        return None, None
    async with ctx.services.sessions() as session:
        cs = await ctx.services.changesets.for_run(session, org_id=ctx.org_id, project_id=ctx.project.id,
                                                   run_id=ctx.run_id, create=False)
        if cs is None:
            return None, None
        return await ctx.services.changesets.overlay(session, cs), str(cs.id)


class RunPythonArgs(ToolArgs):
    code: str = Field(min_length=1, max_length=50_000)
    timeout_s: int = Field(default=60, ge=1, le=600)
    use_changeset: bool = Field(default=False, description="Run against the project with staged changes applied")


class RunPythonTool(Tool):
    name = "run_python"
    description = ("Execute a Python script in the isolated sandbox (no network) with the project files available "
                   "in the working directory. Returns stdout/stderr and exit code.")
    args_model = RunPythonArgs
    permission = PermissionLevel.EXECUTE
    timeout_s = 660
    requires_project = False

    async def run(self, args: RunPythonArgs, ctx: ToolContext) -> ToolOutcome:
        overlay, cs_id = await _overlay_for(ctx, args.use_changeset)
        overlay = {**(overlay or {}), ".ownai_exec.py": args.code}
        result = await ctx.services.sandbox.execute(
            profile="python", argv=["python", ".ownai_exec.py"],
            workspace=ctx.project.root if ctx.project else None, overlay=overlay,
            timeout_s=args.timeout_s, project_label=ctx.project.label if ctx.project else "none",
        )
        status = "ok" if result.ok else "error"
        summary = ("Script exited 0" if result.ok else
                   ("Script timed out" if result.timed_out else f"Script failed with exit code {result.exit_code}"))
        return ToolOutcome(status=status, summary=summary, error_code=None if result.ok else "EXECUTION_FAILED",
                           data=_outcome_from(result, label="run_python") | {"changeset_id": cs_id})


class RunTestsArgs(ToolArgs):
    paths: list[str] = Field(default_factory=list, description="Optional test files/directories to run")
    keyword: str | None = Field(default=None, description="pytest -k expression")
    use_changeset: bool = Field(default=False, description="Test the project with the staged changes applied")
    timeout_s: int = Field(default=300, ge=5, le=900)


class RunTestsTool(Tool):
    name = "run_tests"
    description = ("Run the project's test suite (auto-detected: pytest, npm test, go test, cargo test) in the "
                   "sandbox. Set use_changeset=true to test proposed changes before they are applied.")
    args_model = RunTestsArgs
    permission = PermissionLevel.EXECUTE
    timeout_s = 960

    async def run(self, args: RunTestsArgs, ctx: ToolContext) -> ToolOutcome:
        project = ctx.require_project()
        tests = (project.overview or {}).get("tests") or {}
        command = list(tests.get("command") or [])
        profile = tests.get("profile")
        if not command or not profile:
            return ToolOutcome(status="error", error_code="NO_TEST_SETUP",
                               summary="No test framework was detected in this project, so no tests were run.")
        for p in args.paths:
            command.append(project.jail.normalize(p))
        if args.keyword:
            if tests.get("framework") != "pytest":
                raise ToolError("keyword filtering is only supported for pytest.", code=ErrorCode.TOOL_INVALID_ARGS)
            command += ["-k", args.keyword]
        overlay, cs_id = await _overlay_for(ctx, args.use_changeset)
        result = await ctx.services.sandbox.execute(
            profile=profile, argv=command, workspace=project.root, overlay=overlay,
            timeout_s=args.timeout_s, project_label=project.label,
        )
        output = result.stdout + "\n" + result.stderr
        parsed = parse_pytest(output) if tests.get("framework") == "pytest" else {"counts": {}, "failures": []}
        counts = parsed.get("counts", {})
        if result.timed_out:
            summary, status = f"Tests timed out after {args.timeout_s}s", "error"
        elif parsed.get("no_tests_collected") or (result.exit_code == 5 and tests.get("framework") == "pytest"):
            summary, status = "No tests were collected", "error"
        elif result.exit_code == 0:
            summary, status = "All tests passed" + (f" ({counts.get('passed')} passed)" if counts else ""), "ok"
        else:
            detail = ", ".join(f"{v} {k}" for k, v in counts.items()) or f"exit code {result.exit_code}"
            summary, status = f"Tests failed ({detail})", "error"
        data = _outcome_from(result, label="run_tests", parsed=parsed) | {
            "command": command, "framework": tests.get("framework"), "changeset_id": cs_id,
            "with_changes": overlay is not None,
        }
        return ToolOutcome(status=status, summary=summary, data=data,
                           error_code=None if status == "ok" else "TESTS_FAILED")


class RunLintArgs(ToolArgs):
    paths: list[str] = Field(default_factory=list)
    use_changeset: bool = False


class RunLintTool(Tool):
    name = "run_lint"
    description = "Run the linter (ruff for Python) in the sandbox and return findings."
    args_model = RunLintArgs
    permission = PermissionLevel.EXECUTE
    timeout_s = 200

    async def run(self, args: RunLintArgs, ctx: ToolContext) -> ToolOutcome:
        project = ctx.require_project()
        languages = (project.overview or {}).get("languages", {})
        if "python" not in languages:
            return ToolOutcome(status="error", error_code="LINTER_UNAVAILABLE",
                               summary="Linting is currently supported for Python projects (ruff); none was run.")
        targets = [project.jail.normalize(p) for p in args.paths if PurePosixPath(p).suffix in ("", ".py")] or ["."]
        overlay, cs_id = await _overlay_for(ctx, args.use_changeset)
        result = await ctx.services.sandbox.execute(
            profile="python", argv=["ruff", "check", "--no-cache", "--output-format", "concise", *targets],
            workspace=project.root, overlay=overlay, timeout_s=120, project_label=project.label,
        )
        findings = [m.groupdict() for m in _RUFF_LINE.finditer(result.stdout)][:200]
        if result.exit_code not in (0, 1):
            return ToolOutcome(status="error", error_code="LINTER_FAILED", summary="The linter could not run.",
                               data=_outcome_from(result, label="run_lint"))
        return ToolOutcome(status="ok" if not findings else "error",
                           error_code=None if not findings else "LINT_FINDINGS",
                           summary="No lint findings" if not findings else f"{len(findings)} lint finding(s)",
                           data=_outcome_from(result, label="run_lint") | {"findings": findings, "changeset_id": cs_id})
