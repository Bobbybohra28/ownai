"""Debugging agent: gathers real evidence (test run, logs, relevant code) and produces a DebugReport.

Evidence gathering is deterministic — the failing test output comes from an actual
sandboxed test run, not from the model.
"""

from __future__ import annotations

import re

from app.agents.base import AgentContext, AgentResult, AgentTask, BaseAgent, history_messages, task_prompt
from app.agents.runtimes.single_shot import output_instructions, structured_call
from app.agents.schemas import DebugReport
from app.models.providers.base import ChatMessage
from app.orchestrator.evidence import EvidenceItem
from app.rag.context import build_context

_LOG_REF = re.compile(r"[\w./\-]*(?:logs?/[\w./\-]+|[\w./\-]+\.log)")


class DebuggingAgent(BaseAgent):
    async def execute(self, task: AgentTask, ctx: AgentContext, result: AgentResult) -> None:
        evidence_blocks: list[str] = []
        failure_query = ""
        tests_cfg = (ctx.project.overview.get("tests") or {}) if ctx.project else {}
        if ctx.project and tests_cfg.get("command") and "run_tests" in self.spec.allowed_tools:
            await ctx.status("Running tests to reproduce the problem…")
            outcome = await self.use_tool(ctx, task, "run_tests", {}, result)
            data = outcome.data
            status = "passed" if outcome.ok else ("failed" if data.get("exit_code") not in (None,) else "error")
            ctx.evidence.add(EvidenceItem(kind="tests", status=status, summary=f"Before changes: {outcome.summary}",
                                          step=task.step_id, agent=self.id, ref=outcome.tool_run_id,
                                          data={"applied": False, "phase": "baseline", "counts": data.get("counts"),
                                                "failures": data.get("failures", [])[:10],
                                                "output_tail": (data.get("stdout") or "")[-3000:]}))
            if data:
                tail = (data.get("stdout") or "")[-5000:]
                evidence_blocks.append(f"Test run ({outcome.summary}):\n```\n{tail}\n```")
                failure_query = " ".join(f"{f.get('test', '')} {f.get('message', '')}" for f in data.get("failures", [])[:5])
        for log_path in _LOG_REF.findall(task.request)[:2]:
            if "read_logs" in self.spec.allowed_tools and ctx.project:
                outcome = await self.use_tool(ctx, task, "read_logs", {"path": log_path, "tail": 200}, result)
                if outcome.ok:
                    evidence_blocks.append(f"Log {log_path}:\n```\n{outcome.data.get('lines', '')[-4000:]}\n```")
        context = task.context
        if failure_query and ctx.retriever and ctx.project:
            # focus retrieval on what actually fails
            retrieval = await ctx.retriever.retrieve(f"{task.request} {failure_query}", org_id=ctx.org_id,
                                                     project_id=ctx.project.id, top_k=8)
            pack = build_context(retrieval.chunks, budget_tokens=self.spec.context_budget_tokens)
            if pack.citations:
                context = pack.text
                task = task.model_copy(update={"citations": [c.to_dict() for c in pack.citations]})
                result.output["citations"] = [c.to_dict() for c in pack.citations]
        await ctx.status("Analysing the root cause…")
        extra = "\n\n".join(evidence_blocks) if evidence_blocks else "No automated tests were available to reproduce the issue."
        messages = [
            ChatMessage(role="system", content=self.system_prompt(output_instructions(DebugReport))),
            *history_messages(task),
            ChatMessage(role="user", content=task_prompt(task.model_copy(update={"context": context}), extra=extra)),
        ]
        report = await structured_call(self, ctx, task, messages, DebugReport, result)
        data = report.model_dump()
        result.output.update(data)
        result.summary = report.summary or report.root_cause
        result.text = f"**Root cause:** {report.root_cause}\n\n{report.suggested_fix}".strip()
