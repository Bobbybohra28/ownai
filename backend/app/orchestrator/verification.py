"""Verification of produced code changes: syntax, lint and tests — all actually executed.

Results are recorded in the evidence ledger; nothing is assumed to pass.
"""

from __future__ import annotations

import ast
import json
import uuid
from collections import Counter
from typing import TYPE_CHECKING, Any

import yaml

from app.orchestrator.evidence import EvidenceItem

if TYPE_CHECKING:
    from app.agents.base import AgentContext

VERIFIER = "verifier"


async def changeset_overlay(ctx: AgentContext, changeset_id: str) -> dict[str, str | None]:
    async with ctx.tool_services.sessions() as session:
        cs = await ctx.tool_services.changesets.get(session, uuid.UUID(changeset_id), ctx.org_id)
        return await ctx.tool_services.changesets.overlay(session, cs)


def syntax_errors(overlay: dict[str, str | None]) -> list[str]:
    errors: list[str] = []
    for path, content in overlay.items():
        if content is None:
            continue
        try:
            if path.endswith(".py"):
                ast.parse(content, filename=path)
            elif path.endswith(".json"):
                json.loads(content)
            elif path.endswith((".yml", ".yaml")):
                yaml.safe_load(content)
        except SyntaxError as exc:
            errors.append(f"{path}:{exc.lineno}: {exc.msg}")
        except (json.JSONDecodeError, yaml.YAMLError) as exc:
            errors.append(f"{path}: {str(exc).splitlines()[0]}")
    return errors


async def verify_syntax(ctx: AgentContext, changeset_id: str) -> EvidenceItem:
    overlay = await changeset_overlay(ctx, changeset_id)
    errors = syntax_errors(overlay)
    checked = [p for p, c in overlay.items() if c is not None and p.endswith((".py", ".json", ".yml", ".yaml"))]
    item = EvidenceItem(
        kind="syntax", status="failed" if errors else ("passed" if checked else "skipped"),
        summary=("Syntax errors: " + "; ".join(errors)) if errors else
        (f"Syntax OK for {len(checked)} changed file(s)" if checked else "No files with a supported syntax checker"),
        agent=VERIFIER, ref=changeset_id, data={"errors": errors, "checked": checked},
    )
    ctx.evidence.add(item)
    await ctx.emit("verification", source="syntax", status=item.status, summary=item.summary)
    return item


async def verify_tests(ctx: AgentContext, *, phase: str, changeset_id: str | None) -> EvidenceItem:
    """phase: baseline (no changes) | proposed (changes overlaid in sandbox) | applied (real project)."""
    label = {"proposed": "Testing proposed changes in the sandbox…", "applied": "Running tests after applying changes…",
             "baseline": "Running tests…"}[phase]
    await ctx.status(label)
    await ctx.emit("tool_started", agent=VERIFIER, tool="run_tests", step="verify")
    outcome = await ctx.tools.invoke("run_tests", {"use_changeset": phase == "proposed"},
                                     ctx.tool_context(agent_id=VERIFIER, step_key=f"verify_{phase}"))
    await ctx.emit("tool_finished", agent=VERIFIER, tool="run_tests", step="verify", status=outcome.status,
                   summary=outcome.summary, tool_run_id=outcome.tool_run_id)
    data = outcome.data
    ran = data.get("exit_code") is not None and not data.get("timed_out")
    status = "passed" if outcome.ok else ("failed" if ran else "error")
    item = EvidenceItem(
        kind="tests", status=status, summary=f"{phase}: {outcome.summary}", agent=VERIFIER, ref=outcome.tool_run_id,
        data={"phase": phase, "applied": phase == "applied", "counts": data.get("counts"),
              "failures": data.get("failures", [])[:20], "command": data.get("command"), "changeset_id": changeset_id,
              "error_code": outcome.error_code, "output_tail": (data.get("stdout") or "")[-3000:]},
    )
    ctx.evidence.add(item)
    await ctx.emit("verification", source="tests", phase=phase, status=status, summary=outcome.summary,
                   counts=data.get("counts"), tool_run_id=outcome.tool_run_id)
    return item


async def verify_lint(ctx: AgentContext, changeset_id: str) -> EvidenceItem | None:
    overlay = await changeset_overlay(ctx, changeset_id)
    py_files = [p for p, c in overlay.items() if c is not None and p.endswith(".py")]
    if not py_files or "python" not in (ctx.project.overview.get("languages", {}) if ctx.project else {}):
        return None
    await ctx.status("Linting changed files…")
    tctx = ctx.tool_context(agent_id=VERIFIER, step_key="verify_lint")
    existing = [p for p in py_files if ctx.project and (ctx.project.root / p).is_file()]
    before: Counter[tuple[str, str]] = Counter()
    if existing:
        base = await ctx.tools.invoke("run_lint", {"paths": existing}, tctx)
        if base.status == "error" and base.error_code not in ("LINT_FINDINGS",):
            return None
        before = Counter((f["file"], f["code"]) for f in base.data.get("findings", []))
    after_outcome = await ctx.tools.invoke("run_lint", {"paths": py_files, "use_changeset": True}, tctx)
    if after_outcome.status == "error" and after_outcome.error_code not in ("LINT_FINDINGS",):
        item = EvidenceItem(kind="lint", status="error", summary=f"Lint could not run: {after_outcome.summary}",
                            agent=VERIFIER, ref=after_outcome.tool_run_id)
        ctx.evidence.add(item)
        return item
    after = Counter((f["file"], f["code"]) for f in after_outcome.data.get("findings", []))
    introduced = after - before
    new_findings: list[dict[str, Any]] = [f for f in after_outcome.data.get("findings", [])
                                          if (f["file"], f["code"]) in introduced][:20]
    item = EvidenceItem(
        kind="lint", status="failed" if introduced else "passed",
        summary=(f"{sum(introduced.values())} new lint finding(s) introduced" if introduced
                 else "No new lint findings in changed files"),
        agent=VERIFIER, ref=after_outcome.tool_run_id, data={"new_findings": new_findings},
    )
    ctx.evidence.add(item)
    await ctx.emit("verification", source="lint", status=item.status, summary=item.summary)
    return item
