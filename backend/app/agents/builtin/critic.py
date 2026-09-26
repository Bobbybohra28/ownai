"""Critic/Verifier agent.

Combines deterministic checks on the evidence ledger (did tests run and pass? are cited
sources real?) with an independent model review (a different model is preferred).
Deterministic failures cannot be overridden by the model.
"""

from __future__ import annotations

import re
import uuid
from typing import Any

from app.agents.base import AgentContext, AgentResult, AgentTask, BaseAgent
from app.agents.runtimes.single_shot import output_instructions, structured_call
from app.agents.schemas import CriticIssue, CriticVerdict
from app.core.exceptions import AppError
from app.models.providers.base import ChatMessage
from app.orchestrator.evidence import EvidenceItem

_CITATION = re.compile(r"\[(S\d{1,3})\]")


class CriticAgent(BaseAgent):
    def _deterministic(self, task: AgentTask, ctx: AgentContext) -> list[CriticIssue]:
        issues: list[CriticIssue] = []
        answer = str(task.inputs.get("answer") or "")
        known = {c.get("id") for c in task.citations}
        if answer:
            cited = set(_CITATION.findall(answer))
            unknown = sorted(cited - known)
            if unknown:
                issues.append(CriticIssue(severity="high", category="hallucination",
                                          detail=f"The answer cites sources that were not retrieved: {', '.join(unknown)}."))
            if known and not cited and task.inputs.get("require_citations"):
                issues.append(CriticIssue(severity="medium", category="unsupported_claim",
                                          detail="The answer does not cite any of the retrieved project sources."))
        if task.inputs.get("changeset_id"):
            tests = ctx.evidence.latest("tests", where={"applied": False, "phase": "proposed"})
            if tests is None:
                issues.append(CriticIssue(severity="low", category="tests",
                                          detail="The proposed changes were not tested (no test suite was run)."))
            elif tests.status != "passed":
                issues.append(CriticIssue(severity="critical", category="tests",
                                          detail=f"Tests fail with the proposed changes: {tests.summary}"))
            syntax = ctx.evidence.latest("syntax")
            if syntax is not None and syntax.status == "failed":
                issues.append(CriticIssue(severity="critical", category="syntax", detail=syntax.summary))
            for note in task.inputs.get("unapplied") or []:
                issues.append(CriticIssue(severity="medium", category="requirements", detail=f"Edit not applied: {note}"))
        return issues

    async def _diff(self, ctx: AgentContext, changeset_id: str | None) -> str:
        if not changeset_id:
            return ""
        async with ctx.tool_services.sessions() as session:
            try:
                cs = await ctx.tool_services.changesets.get(session, uuid.UUID(changeset_id), ctx.org_id)
            except (AppError, ValueError):
                return ""
            return (await ctx.tool_services.changesets.full_diff(session, cs))[:12000]

    async def execute(self, task: AgentTask, ctx: AgentContext, result: AgentResult) -> None:
        await ctx.status("Verifying result…")
        deterministic = self._deterministic(task, ctx)
        diff = await self._diff(ctx, task.inputs.get("changeset_id"))
        material: list[str] = []
        if task.inputs.get("answer"):
            material.append(f"Answer to verify:\n{task.inputs['answer'][:6000]}")
        if task.inputs.get("findings"):
            material.append(f"Findings to verify:\n{str(task.inputs['findings'])[:4000]}")
        if diff:
            material.append(f"Proposed diff:\n```diff\n{diff}\n```")
        if task.inputs.get("root_cause"):
            material.append(f"Claimed root cause: {task.inputs['root_cause']}")
        material.append("Evidence recorded by the platform (authoritative):\n" + ctx.evidence.summary_for_prompt())
        criteria = task.inputs.get("success_criteria") or []
        if criteria:
            material.append("Success criteria:\n- " + "\n- ".join(criteria))
        checklist = ("Check: correctness, missing requirements, security problems, edge cases, syntax, whether tests "
                     "support the claims, compatibility, hallucinated APIs/files, and unsupported assumptions. "
                     "Use 'fail' only for concrete problems you can point to; 'uncertain' when evidence is insufficient.")
        messages = [
            ChatMessage(role="system", content=self.system_prompt(checklist + "\n" + output_instructions(CriticVerdict))),
            ChatMessage(role="user", content=f"User request:\n{task.request}\n\n" + "\n\n".join(material)
                        + (f"\n\n<context>\n{task.context[:8000]}\n</context>" if task.context else "")),
        ]
        avoid = set(task.inputs.get("producer_models") or [])
        model_verdict: CriticVerdict | None = None
        try:
            model_verdict = await structured_call(self, ctx, task, messages, CriticVerdict, result, avoid_models=avoid)
        except AppError as exc:
            result.notices.append(f"Model-based review unavailable: {exc.message}")
        issues = deterministic + (model_verdict.issues if model_verdict else [])
        if any(i.severity == "critical" for i in deterministic):
            verdict = "fail"
        elif model_verdict is None or model_verdict.verdict == "pass" and any(i.severity == "high" for i in deterministic):
            verdict = "uncertain"
        else:
            verdict = model_verdict.verdict
        summary = (model_verdict.summary if model_verdict and model_verdict.summary else
                   f"{len(issues)} issue(s) found" if issues else "No issues found")
        final = CriticVerdict(verdict=verdict, issues=issues,  # type: ignore[arg-type]
                              unmet_requirements=model_verdict.unmet_requirements if model_verdict else [],
                              summary=summary)
        result.output = final.model_dump()
        result.summary = f"Verdict: {verdict} — {summary}"
        ctx.evidence.add(EvidenceItem(kind="critic", status={"pass": "passed", "fail": "failed"}.get(verdict, "info"),
                                      summary=result.summary[:500], step=task.step_id, agent=self.id,
                                      data={"verdict": verdict, "issues": len(issues),
                                            "independent": bool(model_verdict) and not (set(result.model_ids) & avoid)}))
        await ctx.emit("verification", source="critic", verdict=verdict, summary=summary,
                       issues=[i.model_dump() for i in issues][:20])


def critic_inputs(step_outputs: dict[str, Any]) -> dict[str, Any]:
    """Collect what the critic should review from earlier step outputs."""
    inputs: dict[str, Any] = {}
    for output in step_outputs.values():
        if not isinstance(output, dict):
            continue
        if output.get("answer"):
            inputs["answer"] = output["answer"]
        if output.get("findings"):
            inputs["findings"] = output["findings"]
        if output.get("root_cause"):
            inputs["root_cause"] = output["root_cause"]
        if output.get("changeset_id"):
            inputs["changeset_id"] = output["changeset_id"]
            inputs["unapplied"] = output.get("unapplied", [])
    return inputs
