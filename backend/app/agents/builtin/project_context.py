"""Project Context Agent: retrieves only the relevant project context for a task.

Deterministic (no LLM call): hybrid retrieval + project overview, assembled into a
token-budgeted, citable context pack. It never sends the whole project to a model.
"""

from __future__ import annotations

from typing import Any

from app.agents.base import AgentContext, AgentResult, AgentTask, BaseAgent
from app.core.exceptions import AgentError, ErrorCode
from app.rag.context import build_context

MODE_TOP_K = {"quick": 5, "developer": 8, "debug": 8, "review": 10, "architecture": 12, "deep": 12, "project": 14}


def overview_header(overview: dict[str, Any], *, include_tree: bool) -> str:
    if not overview:
        return ""
    lines = ["Project overview:"]
    if overview.get("languages"):
        lines.append("- Languages: " + ", ".join(f"{k} ({v})" for k, v in list(overview["languages"].items())[:8]))
    if overview.get("frameworks"):
        lines.append("- Frameworks/tools: " + ", ".join(overview["frameworks"][:15]))
    tests = overview.get("tests") or {}
    if tests.get("framework"):
        lines.append(f"- Tests: {tests['framework']} ({len(tests.get('test_files', []))} test files)")
    if overview.get("endpoints"):
        eps = [f"{e['method']} {e['path']} ({e['file']}:{e['line']})" for e in overview["endpoints"][:15]]
        lines.append("- API endpoints: " + "; ".join(eps))
    if overview.get("env_vars"):
        lines.append("- Environment variables used (names only): " + ", ".join(overview["env_vars"][:30]))
    if include_tree and overview.get("tree"):
        tree = "\n".join(overview["tree"].splitlines()[:80])
        lines.append("- File tree:\n" + tree)
    return "\n".join(lines)


class ProjectContextAgent(BaseAgent):
    async def execute(self, task: AgentTask, ctx: AgentContext, result: AgentResult) -> None:
        if ctx.project is None:
            result.status = "skipped"
            result.summary = "No project selected; answering without project context."
            return
        if ctx.retriever is None:
            raise AgentError("The project index is not available.", code=ErrorCode.RAG_ERROR)
        await ctx.status("Searching project…")
        top_k = int(task.inputs.get("top_k") or MODE_TOP_K.get(ctx.mode, 8))
        query = task.request if task.goal == task.request else f"{task.request}\n{task.goal}"
        feedback = task.feedback or ""
        retrieval = await ctx.retriever.retrieve(query + ("\n" + feedback if feedback else ""), org_id=ctx.org_id,
                                                 project_id=ctx.project.id, top_k=top_k)
        include_tree = ctx.mode in ("project", "architecture", "deep") or task.intent == "architecture"
        header = overview_header(ctx.project.overview, include_tree=include_tree)
        budget = self.spec.context_budget_tokens if ctx.mode != "project" else self.spec.context_budget_tokens * 2
        pack = build_context(retrieval.chunks, budget_tokens=budget, header=header)
        result.output = {
            "context": pack.text,
            "citations": [c.to_dict() for c in pack.citations],
            "retrieval": {"semantic": retrieval.used_dense, "lexical": retrieval.used_lexical,
                          "reranked": retrieval.used_rerank, "notices": retrieval.notices},
        }
        result.notices.extend(retrieval.notices)
        files = sorted({c.file_path for c in pack.citations})
        result.summary = (f"Found {len(pack.citations)} relevant passage(s) in {len(files)} file(s)"
                          if pack.citations else "No relevant passages found in the project index.")
        await ctx.emit("sources", citations=[c.to_dict() for c in pack.citations])
        for notice in retrieval.notices:
            await ctx.emit("notice", level="info", message=notice)
