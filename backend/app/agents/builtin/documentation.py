"""Documentation agent: writes documentation from project context; if the user asks for a
file, the document is staged in a change set (applied only after approval)."""

from __future__ import annotations

import re

from app.agents.base import AgentContext, AgentResult, AgentTask, BaseAgent, history_messages, task_prompt
from app.agents.runtimes.single_shot import output_instructions, structured_call
from app.agents.schemas import DocumentationOutput
from app.models.providers.base import ChatMessage

_WRITE_INTENT = re.compile(r"(?i)\b(create|write|update|generate|add)\b.*\b(readme|docs?|documentation|\.md)\b")


class DocumentationAgent(BaseAgent):
    async def execute(self, task: AgentTask, ctx: AgentContext, result: AgentResult) -> None:
        await ctx.status("Writing documentation…")
        guidance = ("Write accurate Markdown documentation grounded in the provided context. Cite context blocks as "
                    "[S#] where helpful. If the user asked for a file, set target_path to a project-relative path "
                    "(e.g. docs/auth.md); otherwise leave it null.\n" + output_instructions(DocumentationOutput))
        messages = [
            ChatMessage(role="system", content=self.system_prompt(guidance)),
            *history_messages(task),
            ChatMessage(role="user", content=task_prompt(task)),
        ]
        doc = await structured_call(self, ctx, task, messages, DocumentationOutput, result)
        result.output = doc.model_dump()
        result.text = doc.content
        result.summary = doc.title
        if doc.target_path and ctx.project and _WRITE_INTENT.search(task.request):
            outcome = await self.use_tool(ctx, task, "write_file", {"path": doc.target_path, "content": doc.content}, result)
            if outcome.ok:
                result.changeset_id = outcome.data.get("changeset_id")
                result.output["changeset_id"] = result.changeset_id
                result.output["files"] = [outcome.data.get("path")]
            else:
                result.notices.append(f"Could not stage {doc.target_path}: {outcome.summary}")
